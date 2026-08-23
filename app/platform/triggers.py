"""Trigger routing: inbound events are matched against trigger rows (data),
not code paths.

One pipeline for every inbound event, in this order:
  1. an open escalation on the work item (or via a matching instance trigger)
     -> the event is the human's answer / completion note; resolve and resume;
  2. a matching instance trigger (goal-bound, e.g. a conversation_key from the
     agent's own outbound comms) -> wake that goal's run with the new info;
  3. an active run on the work item -> already running, ignore;
  4. a standing trigger (config-bound, e.g. the agent's handle in a staff
     message) -> open a new goal and start its first run.

Standing triggers replace the hardcoded agent name in the webhook; instance
triggers are minted by the Toolset on outbound comms so replies route back.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import select

from app.platform.db import SessionLocal
from app.platform.models import (
    AgentRun, Communication, Escalation, EscalationStatus, Goal, RunEvent,
    RunStatus, Trigger,
)

log = logging.getLogger("triggers")


def _event(db, run_id: int, firm_id: int, summary: str, detail: dict | None = None) -> None:
    db.add(RunEvent(run_id=run_id, firm_id=firm_id, kind="trigger",
                    summary=summary,
                    detail=json.dumps(detail or {}, default=str)))


def _wake_run(db, run: AgentRun, note: str) -> None:
    """Put new information where the agent will see it and make the run due."""
    if run.status not in (RunStatus.PENDING, RunStatus.WAITING, RunStatus.ESCALATED):
        return
    scratch = json.loads(run.scratchpad) if run.scratchpad else {}
    scratch.setdefault("inbound_messages", []).append(note)
    run.scratchpad = json.dumps(scratch)
    run.status = RunStatus.WAITING
    run.next_run_at = datetime.now(timezone.utc)


def _find_open_escalation(db, firm_id: int, task_ref: str | None,
                          conversation_key: str | None = None) -> Escalation | None:
    if conversation_key:
        esc = db.scalar(
            select(Escalation).join(AgentRun, Escalation.run_id == AgentRun.id)
            .where(Escalation.firm_id == firm_id,
                   Escalation.status == EscalationStatus.OPEN,
                   AgentRun.scratchpad.contains(conversation_key))
        )
        if esc is not None:
            return esc
    if task_ref:
        return db.scalar(
            select(Escalation).where(Escalation.firm_id == firm_id,
                                     Escalation.task_ref == task_ref,
                                     Escalation.status == EscalationStatus.OPEN)
        )
    return None


def _match_instance_trigger(db, firm_id: int, event_type: str, event: dict) -> Trigger | None:
    key = event.get("conversation_key")
    if not key:
        return None
    rows = db.scalars(
        select(Trigger).where(Trigger.firm_id == firm_id, Trigger.event_type == event_type,
                              Trigger.enabled.is_(True), Trigger.goal_id.isnot(None))
    ).all()
    for t in rows:
        if t.match().get("conversation_key") == key:
            return t
    return None


def _match_standing_trigger(db, firm_id: int, event_type: str, event: dict) -> Trigger | None:
    rows = db.scalars(
        select(Trigger).where(Trigger.firm_id == firm_id, Trigger.event_type == event_type,
                              Trigger.enabled.is_(True), Trigger.goal_id.is_(None))
    ).all()
    message = (event.get("message") or "")
    for t in rows:
        m = t.match()
        handle = m.get("handle")
        if handle and handle in message:
            return t
        if not handle and m.get("match_all"):
            return t
    return None


def route_event(event_type: str, event: dict) -> dict:
    """The one routing pipeline. `event` carries firm_id plus event-specific
    fields (case_ref, task_ref, message, conversation_key, sender, subject)."""
    from app.platform.runtime import execute_run, open_goal, resolve_escalation

    firm_id = int(event["firm_id"])
    task_ref = event.get("task_ref")
    conv_key = event.get("conversation_key")
    text = (event.get("message") or "").strip()
    sender = event.get("author") or event.get("sender") or "someone"

    with SessionLocal() as db:
        # 1. open escalation on this work item (or keyed to this conversation)
        esc = _find_open_escalation(db, firm_id, task_ref, conv_key)
        if esc is not None and text and sender != "agent":
            esc_id, run_id = esc.id, esc.run_id
            log.info("event %s answers escalation #%s", event_type, esc_id)
        else:
            esc_id = None

    if esc_id is not None:
        resolve_escalation(esc_id, text)
        execute_run(run_id)
        return {"handled": "escalation_answered", "escalation_id": esc_id, "run_id": run_id}

    with SessionLocal() as db:
        # 2. instance trigger: a reply on one of the agent's own conversations
        trig = _match_instance_trigger(db, firm_id, event_type, event)
        if trig is not None:
            goal = db.get(Goal, trig.goal_id)
            run = db.scalar(
                select(AgentRun).where(AgentRun.goal_id == trig.goal_id)
                .order_by(AgentRun.id.desc())
            )
            if goal is not None and run is not None and run.status in (
                    RunStatus.PENDING, RunStatus.WAITING, RunStatus.ESCALATED):
                _wake_run(db, run, f"[inbound {event_type} from {sender}] {text}")
                _event(db, run.id, firm_id,
                       f"inbound {event_type} routed to run #{run.id} via trigger #{trig.id}",
                       {"sender": sender, "text": text[:500]})
                db.commit()
                run_id = run.id
            else:
                run_id = None
        else:
            run_id = None

    if run_id is not None:
        execute_run(run_id)
        return {"handled": "inbound_routed", "run_id": run_id, "trigger_id": trig.id}

    with SessionLocal() as db:
        # 3. already an active run on this work item -> ignore
        if task_ref:
            existing = db.scalar(
                select(AgentRun).where(
                    AgentRun.firm_id == firm_id, AgentRun.task_ref == task_ref,
                    AgentRun.status.in_([RunStatus.PENDING, RunStatus.WAITING, RunStatus.ESCALATED]),
                )
            )
            if existing is not None:
                return {"handled": "already_running", "run_id": existing.id}

        # 4. standing trigger -> open a new goal
        standing = _match_standing_trigger(db, firm_id, event_type, event)
        if standing is None:
            return {"handled": "ignored"}
        from app.platform.models import AgentConfig
        cfg = db.get(AgentConfig, standing.agent_config_id)
        if cfg is None or not cfg.enabled:
            return {"handled": "ignored", "reason": "agent config disabled"}

    goal, run = open_goal(
        firm_id=firm_id,
        case_ref=str(event.get("case_ref") or ""),
        task_ref=task_ref,
        agent_config_id=cfg.id,
        brief=text or f"{event_type} for {cfg.agent_name}",
    )
    with SessionLocal() as db:
        _event(db, run.id, firm_id,
               f"goal #{goal.id} opened by trigger #{standing.id} ({event_type})",
               {"handle": cfg.handle})
        db.commit()
    execute_run(run.id)
    return {"handled": "goal_opened", "goal_id": goal.id, "run_id": run.id}


def mint_reply_trigger(run: AgentRun, conversation_key: str) -> None:
    """Instance trigger: a reply on this conversation wakes this run's goal."""
    if not run.goal_id:
        return
    with SessionLocal() as db:
        rows = db.scalars(
            select(Trigger).where(Trigger.goal_id == run.goal_id,
                                  Trigger.event_type == "inbound_email",
                                  Trigger.enabled.is_(True))
        ).all()
        if any(t.match().get("conversation_key") == conversation_key for t in rows):
            return
        db.add(Trigger(
            firm_id=run.firm_id, goal_id=run.goal_id, event_type="inbound_email",
            match_json=json.dumps({"conversation_key": conversation_key}), enabled=True,
        ))
        db.commit()
