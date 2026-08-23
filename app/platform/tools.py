"""Platform tools available to agents.

Every tool is a thin client over the firm's CMS connector or the comms/portal
services, and every call is written to the run's audit trail. Agents receive a
Toolset scoped to their run, so firm scoping and the audit trail are automatic
— there is no tool path that bypasses them.

Outbound comms also write into the **communications archive** (the platform's
private, firm-tagged copy of what the agent sent and heard — a derivative
context store, not a system of record) and mint a ``conversation_key`` with a
reply trigger, so inbound replies route back to the owning run.

Tool results are structured but deliberately free-form in their payloads:
the CMS changes between firms, so agents must read what they get rather than
assume a fixed schema.
"""

from __future__ import annotations

import json
import uuid
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import select

from app.config import settings
from app.platform.connectors import CMSConnector
from app.platform.models import Communication, RunEvent
from app.platform.db import SessionLocal

if TYPE_CHECKING:
    from app.platform.models import AgentRun


def _post(url: str, payload: dict) -> dict:
    resp = httpx.post(url, json=payload, timeout=10)
    resp.raise_for_status()
    return resp.json()


def _get(url: str) -> dict:
    resp = httpx.get(url, timeout=10)
    resp.raise_for_status()
    return resp.json()


class Toolset:
    def __init__(self, run: "AgentRun", connector: CMSConnector | None = None):
        self.run = run
        self.connector = connector

    # -- audit trail -------------------------------------------------

    def _log(self, kind: str, summary: str, detail: Any = None) -> None:
        with SessionLocal() as db:
            db.add(
                RunEvent(
                    run_id=self.run.id,
                    firm_id=self.run.firm_id,
                    kind=kind,
                    summary=summary,
                    detail=json.dumps(detail, default=str) if detail is not None else "",
                )
            )
            db.commit()

    # -- communications archive -----------------------------------------

    def _archive(self, channel: str, direction: str, counterparty: str,
                 conversation_key: str, summary: str, content: str,
                 subject: str = "") -> None:
        with SessionLocal() as db:
            db.add(Communication(
                firm_id=self.run.firm_id, case_ref=self.run.case_ref,
                goal_id=self.run.goal_id, run_id=self.run.id,
                channel=channel, direction=direction, counterparty=counterparty,
                conversation_key=conversation_key, subject=subject,
                summary=summary, content=content,
            ))
            db.commit()

    # -- CMS (through the firm's connector) ------------------------------

    def _cms(self) -> CMSConnector:
        if self.connector is None:
            raise RuntimeError("Toolset has no CMS connector (run created without firm binding)")
        return self.connector

    def cms_get_case(self) -> dict:
        return self._cms().get_case(self.run.case_ref)

    def cms_get_task(self) -> dict:
        if not self.run.task_ref:
            return {}
        return self._cms().get_task(self.run.task_ref)

    def cms_write_task(self, status: str | None = None, note: str | None = None) -> dict:
        """Write outcomes back to the CMS task - the CMS stays the system of record."""
        result = self._cms().write_task(self.run.task_ref, status=status, note=note)
        self._log("tool_call", f"CMS write-back on task {self.run.task_ref}: status={status} note={note!r}", result)
        return result

    def cms_post_message(self, body: str, author: str = "agent") -> dict:
        result = self._cms().post_message(self.run.task_ref, body, author=author)
        self._log("tool_call", f"posted to CMS chat on task {self.run.task_ref}: {body!r}", result)
        return result

    def cms_create_task(self, title: str, notes: str = "") -> dict:
        result = self._cms().create_task(self.run.case_ref, title, notes)
        self._log("tool_call", f"created CMS task on case {self.run.case_ref}: {title!r}", result)
        return result

    # -- comms --------------------------------------------------------

    def voice_call(self, to: str, purpose: str, scenario: str | None = None) -> dict:
        """Place a phone call. The platform does a real TTS->STT round trip:

        1. synthesize the agent's opening line (TTS),
        2. let the stub supply the other party's scripted reply,
        3. synthesize that reply in the other party's voice (TTS),
        4. transcribe it back (STT) - the transcript the agent reads.

        The outcome still comes from the scripted scenario (deterministic demo);
        speech replaces the text-transcript path so the calling setup is real.
        """
        scenario = scenario or f"provider:{to}"
        conversation_key = uuid.uuid4().hex[:16]
        result = _post(
            f"{settings.voice_stub_url}/call",
            {
                "scenario": scenario,
                "to": to,
                "script_prompt": purpose,
                "firm_id": self.run.firm_id,
                "case_ref": self.run.case_ref,
            },
        )
        # speech round-trip (best-effort; falls back to the scripted text)
        try:
            from app.platform import llm
            them_text = result["transcript"][-1]["text"]
            agent_audio = llm.synthesize(purpose, settings.mistral_tts_voice)
            them_audio = llm.synthesize(them_text, settings.mistral_tts_provider_voice)
            heard = llm.transcribe(them_audio)
            if heard:
                result["transcript"][-1]["text"] = heard
            result["audio"] = {
                "agent_tts_bytes": len(agent_audio),
                "them_tts_bytes": len(them_audio),
                "stt_transcript": heard,
                "stt_used": bool(heard),
            }
        except Exception as exc:  # noqa: BLE001 - speech is best-effort in the POC
            result["audio"] = {"error": str(exc)}
        result["conversation_key"] = conversation_key
        transcript_text = "\n".join(f"{t['speaker']}: {t['text']}" for t in result.get("transcript", []))
        self._archive("voice", "outbound", to, conversation_key,
                      f"call to {to}: outcome={result.get('outcome')}", transcript_text)
        self._log(
            "tool_call",
            f"voice call to {to} ({scenario}): outcome={result.get('outcome')}",
            result,
        )
        return result

    def send_email(self, to: str, subject: str, body: str, scenario: str | None = None) -> dict:
        conversation_key = uuid.uuid4().hex[:16]
        scenario = scenario or f"email:{to}"
        result = _post(
            f"{settings.email_stub_url}/send",
            {
                "scenario": scenario,
                "to": to,
                "subject": subject,
                "body": body,
                "firm_id": self.run.firm_id,
                "case_ref": self.run.case_ref,
                "conversation_key": conversation_key,
            },
        )
        result["conversation_key"] = conversation_key
        content = f"SUBJECT: {subject}\n\n{body}"
        reply = result.get("reply")
        if reply:
            content += f"\n\n--- immediate reply ---\n{reply}"
        self._archive("email", "outbound", to, conversation_key,
                      f"email to {to}: {subject!r} -> {result.get('outcome')}", content,
                      subject=subject)
        # a reply on this conversation should wake this goal's run
        from app.platform.triggers import mint_reply_trigger
        mint_reply_trigger(self.run, conversation_key)
        self._log("tool_call", f"email to {to}: {subject!r} -> {result.get('outcome')}", result)
        return result

    def send_fax(self, to: str, document: str) -> dict:
        conversation_key = uuid.uuid4().hex[:16]
        result = _post(
            f"{settings.fax_stub_url}/send",
            {"to": to, "document": document, "firm_id": self.run.firm_id,
             "case_ref": self.run.case_ref, "conversation_key": conversation_key},
        )
        result["conversation_key"] = conversation_key
        self._archive("fax", "outbound", to, conversation_key,
                      f"fax to {to}", document)
        self._log("tool_call", f"fax to {to}: {document!r}", result)
        return result

    # -- memory: communications archive + skill library ------------------

    def search_conversations(self, query: str, channel: str | None = None,
                             counterparty: str | None = None, limit: int = 10) -> dict:
        """Keyword search over this firm's communications archive (POC search;
        production adds vector retrieval). Firm-scoped by construction."""
        words = [w.lower() for w in query.split() if len(w) >= 3]
        with SessionLocal() as db:
            q = select(Communication).where(Communication.firm_id == self.run.firm_id)
            if channel:
                q = q.where(Communication.channel == channel)
            rows = db.scalars(q.order_by(Communication.id.desc()).limit(200)).all()
        hits = []
        for c in rows:
            hay = f"{c.counterparty} {c.subject} {c.summary} {c.content}".lower()
            if counterparty and counterparty.lower() not in c.counterparty.lower():
                continue
            if words and not any(w in hay for w in words):
                continue
            hits.append({
                "id": c.id, "channel": c.channel, "direction": c.direction,
                "counterparty": c.counterparty, "subject": c.subject,
                "summary": c.summary, "occurred_at": c.occurred_at.isoformat(),
            })
            if len(hits) >= limit:
                break
        result = {"matches": hits, "searched": query}
        self._log("tool_call", f"searched conversations for {query!r}: {len(hits)} hit(s)")
        return result

    def read_conversation(self, communication_id: int) -> dict:
        with SessionLocal() as db:
            c = db.get(Communication, communication_id)
            if c is None or c.firm_id != self.run.firm_id:
                return {"error": "not found"}
            result = {
                "id": c.id, "channel": c.channel, "direction": c.direction,
                "counterparty": c.counterparty, "subject": c.subject,
                "summary": c.summary, "content": c.content,
                "occurred_at": c.occurred_at.isoformat(),
            }
        self._log("tool_call", f"read conversation #{communication_id} ({result['channel']})")
        return result

    def load_skill(self, name: str, allowed: list[str]) -> dict:
        """Progressive disclosure: load the body of an attached skill. The
        allow-list comes from the firm's agent config; loads are audited."""
        from app.platform import skills as skill_lib

        meta = skill_lib.get(name)
        if meta is None or name not in allowed:
            self._log("skill_load", f"skill {name!r} requested but not available")
            return {"error": f"skill {name!r} is not available to this agent"}
        self._log("skill_load", f"loaded skill {name!r}", {"path": meta.path})
        return {"name": meta.name, "content": meta.body}

    # -- provider portal (browser-automation path) ---------------------

    def portal_request_records(self, provider_key: str, client_name: str, case_number: str) -> dict:
        result = _post(
            f"{settings.portal_stub_url}/requests",
            {
                "provider_key": provider_key,
                "client_name": client_name,
                "case_number": case_number,
                "hipaa_on_file": True,
                "firm_id": self.run.firm_id,
                "case_ref": self.run.case_ref,
            },
        )
        self._log(
            "tool_call",
            f"portal records request for {client_name} at {provider_key}: {result.get('status')}",
            result,
        )
        return result

    def portal_check_request(self, request_id: int) -> dict:
        result = _get(f"{settings.portal_stub_url}/requests/{request_id}")
        self._log("tool_call", f"portal request #{request_id} status: {result.get('status')}", result)
        return result
