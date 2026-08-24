"""Stub communication channels: voice, email, fax.

Each call/email/fax is played against a scripted scenario so demos are
repeatable. When MISTRAL_API_KEY is set, the stub can also live-generate the
other party's response so the demo feels like real progress instead of a
replayed tape. Everything is logged and inspectable from the dashboard.

The voice stub doubles as the 'LLM': it returns a structured outcome
(answered / status / records_ready / refused / needs_payment ...) which the
agent reasons over. In production that structure would come from the model;
here it comes from the scenario script or from live LLM generation.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/stubs", tags=["stubs"])


def _conn() -> sqlite3.Connection:
    path = settings.database_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


def _log(channel: str, payload: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        cur = conn.execute(
            "INSERT INTO stub_log (channel, payload, created_at) VALUES (?, ?, ?)",
            (channel, json.dumps(payload), now),
        )
        conn.commit()
        row_id = cur.lastrowid
    return {"id": row_id, "channel": channel, "logged_at": now}


def _advance_scenario(name: str) -> dict:
    """Pop the next scripted turn for a scenario; stick on the last turn."""
    with _conn() as conn:
        row = conn.execute("SELECT script, step FROM stub_scenarios WHERE name = ?", (name,)).fetchone()
        if row is None:
            return {"outcome": "no_answer", "spoken": "(no scenario configured)"}
        script = json.loads(row["script"])
        step = row["step"]
        turn = script[min(step, len(script) - 1)]
        if step < len(script) - 1:
            conn.execute("UPDATE stub_scenarios SET step = ? WHERE name = ?", (step + 1, name))
            conn.commit()
        return turn


def _parse_llm_json(content: str) -> dict | None:
    """Mistral often wraps JSON in markdown fences; strip them and parse."""
    if not content:
        return None
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None


def _peek_scenario(name: str) -> list[dict]:
    """Read the static scenario script WITHOUT advancing it.

    The seeded script is the persona's starting point: the live generator uses
    it as reference material so improvisations stay consistent with the seeded
    client/provider story."""
    with _conn() as conn:
        row = conn.execute("SELECT script FROM stub_scenarios WHERE name = ?", (name,)).fetchone()
        if row is None:
            return []
        try:
            return json.loads(row["script"])
        except ValueError:
            return []


def _recent_comms_context(counterparty: str, limit: int = 3) -> list[str]:
    """Recent archived communications with this counterparty, newest first.

    Gives the live generator call-to-call continuity: the client can reference
    what was said last time instead of every call sounding like the first."""
    if not counterparty:
        return []
    with _conn() as conn:
        try:
            rows = conn.execute(
                "SELECT channel, direction, summary, occurred_at FROM communications "
                "WHERE counterparty = ? ORDER BY id DESC LIMIT ?",
                (counterparty, limit),
            ).fetchall()
        except sqlite3.Error:
            return []
    return [f"{r['occurred_at']} {r['channel']} ({r['direction']}): {r['summary']}" for r in rows]


def _case_context(case_ref: str) -> dict:
    """The actual CMS case this call is about — the per-client, per-case
    static starting point: who the client is, what happened to them, where the
    case stands, and who the contacts are (with roles)."""
    if not case_ref:
        return {}
    with _conn() as conn:
        try:
            case = conn.execute(
                "SELECT id, case_number, client_name, case_type, status, summary "
                "FROM cms_cases WHERE id = ?",
                (int(case_ref),),
            ).fetchone()
            if case is None:
                return {}
            contacts = conn.execute(
                "SELECT role, name, phone, email, details FROM cms_contacts WHERE case_id = ?",
                (case["id"],),
            ).fetchall()
        except (sqlite3.Error, ValueError):
            return {}
    return {
        "case_number": case["case_number"],
        "client_name": case["client_name"],
        "case_type": case["case_type"],
        "status": case["status"],
        "summary": case["summary"],
        "contacts": [dict(c) for c in contacts],
    }


def _counterparty_descriptor(case: dict, counterparty: str) -> str:
    """Who is the called/emailed party ON THIS CASE? Match phone/email against
    the case contacts so the improviser knows whether it is playing the client
    or a provider — and which one."""
    for c in case.get("contacts", []):
        if counterparty and counterparty in (c.get("phone") or "", c.get("email") or ""):
            return f"{c.get('role')} — {c.get('name')} ({c.get('details', 'no details')})"
    return "someone related to the case (no matching contact record)"


def _live_improvise(scenario: str, counterparty: str, channel: str,
                    agent_said: str, case_ref: str = "") -> dict | None:
    """Improvise the other party's reply with Mistral, grounded in the actual
    CMS case (per-client, per-case static starting point), the seeded scenario
    lines (how this person talks), and recent communications (continuity).
    Returns the raw JSON turn, or None if unavailable."""
    if not settings.mistral_api_key:
        return None
    try:
        from app.platform.llm import chat
        case = _case_context(case_ref)
        case_text = json.dumps(case, default=str) or "(no case context available)"
        who = _counterparty_descriptor(case, counterparty)
        reference = json.dumps(_peek_scenario(scenario), default=str) or "(none)"
        history = "\n".join(_recent_comms_context(counterparty)) or "(no prior interactions)"
        system = f"""You are improvising the OTHER PARTY in a {channel} exchange for a realistic legal-tech demo.
A law-firm agent is reaching out; you play the person they contacted.

THE CASE (your static starting point — stay consistent with it):
{case_text}

WHO YOU ARE ON THIS CASE: {who}
- If you are the CLIENT: you are the injured person on this case. Talk about YOUR
  injury, YOUR treatment, YOUR life right now, and questions about YOUR case.
- If you are a PROVIDER/records desk: you handle records requests for this case.
  Talk about the request status, what you need (payment, faxed forms), and turnaround.

SEEDED VOICE LINES (how this person talks — riff on these, don't repeat them):
{reference}

RECENT PAST INTERACTIONS with the firm (newest first; reference them naturally if relevant):
{history}

RULES:
- 1-3 sentences of natural, everyday wording. Vary phrasing and mood from one
  exchange to the next — never repeat a past reply verbatim.
- Small new details are welcome when consistent with the case (a bill that
  arrived, a doctor's appointment, a symptom change, a question for the firm).
- outcome must be one of: answered | records_ready | needs_payment | needs_fax |
  refused | no_answer | voicemail. Clients usually answer; providers answer.
- Return ONLY raw JSON: {{"outcome": "...", "spoken": "...", "structured": {{}}}}"""
        msg = chat([
            {"role": "system", "content": system},
            {"role": "user", "content": f"The agent says: {agent_said}"},
        ])
        return _parse_llm_json(msg.content)
    except Exception:
        return None


# ---------- voice ----------

class CallIn(BaseModel):
    scenario: str = "default"
    to: str = ""
    script_prompt: str = ""   # what the agent 'says' it wants
    firm_id: int = 0
    case_ref: str = ""


@router.post("/voice/call")
def make_call(body: CallIn):
    # Improvise the reply live (grounded in the seeded persona) when Mistral is
    # available; fall back to replaying the static script otherwise.
    turn = _live_improvise(body.scenario, body.to, "phone call",
                           body.script_prompt or "Hello, following up with you.",
                           case_ref=body.case_ref)
    if turn is None:
        turn = _advance_scenario(body.scenario)
    transcript = [
        {"speaker": "agent", "text": body.script_prompt or "Hello, following up on a records request."},
        {"speaker": "them", "text": turn.get("spoken", "...")},
    ]
    result = {
        "outcome": turn.get("outcome", "no_answer"),
        "transcript": transcript,
        "structured": turn.get("structured", {}),
        "to": body.to,
    }
    logged = _log("voice", {**body.model_dump(), "result": result})
    return {"call_id": logged["id"], **result}


# ---------- email ----------

class EmailIn(BaseModel):
    scenario: str = "default"
    to: str = ""
    subject: str = ""
    body: str = ""
    firm_id: int = 0
    case_ref: str = ""
    conversation_key: str = ""


@router.post("/email/send")
def send_email(body: EmailIn):
    turn = _live_improvise(body.scenario, body.to, "email",
                           f"Subject: {body.subject}\n\n{body.body}",
                           case_ref=body.case_ref)
    if turn is None:
        turn = _advance_scenario(body.scenario)
    result = {
        "delivered": True,
        "reply": turn.get("spoken", ""),
        "outcome": turn.get("outcome", "sent"),
        "structured": turn.get("structured", {}),
        "conversation_key": body.conversation_key,
    }
    logged = _log("email", {**body.model_dump(), "result": result})
    return {"email_id": logged["id"], **result}


class InboundEmailIn(BaseModel):
    """Simulate the outside world: an email arrives for the platform (a staff
    answer by email, or a provider's reply to the agent's email). The stub
    forwards it to the platform's inbound-email webhook, echoing the
    conversation_key the agent's outbound email minted."""
    firm_id: int
    sender: str
    subject: str = ""
    body: str
    conversation_key: str = ""
    case_ref: str | None = None
    task_ref: str | None = None
    forward: bool = True     # False = just log it (dashboard form posts with True)


@router.post("/email/inbound")
def inbound_email(body: InboundEmailIn):
    logged = _log("email_inbound", body.model_dump())
    routed: dict = {"forwarded": False}
    if body.forward:
        import httpx
        try:
            resp = httpx.post(
                f"{settings.cms_base_url}/api/webhooks/inbound-email",
                json=body.model_dump(exclude={"forward"}),
                timeout=15,
            )
            routed = {"forwarded": True, "platform": resp.json()}
        except httpx.HTTPError as exc:
            routed = {"forwarded": False, "error": str(exc)}
    return {"inbound_id": logged["id"], **routed}


# ---------- fax ----------

class FaxIn(BaseModel):
    to: str = ""
    document: str = ""
    firm_id: int = 0
    case_ref: str = ""
    conversation_key: str = ""


@router.post("/fax/send")
def send_fax(body: FaxIn):
    logged = _log("fax", {**body.model_dump(), "result": {"delivered": True}})
    return {"fax_id": logged["id"], "delivered": True}
