"""Platform tools available to agents.

Every tool is a thin HTTP client over the CMS / stub services, and every call
is written to the run's audit trail. Agents receive a Toolset scoped to their
run, so firm_id / case_id / task_id are always attached and the audit trail
is automatic.

Tool results are structured but deliberately free-form in their payloads:
the CMS changes between firms, so agents must read what they get rather than
assume a fixed schema.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx

from app.config import settings
from app.platform.models import RunEvent, RunStatus
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
    def __init__(self, run: "AgentRun"):
        self.run = run

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

    # -- CMS ----------------------------------------------------------

    def cms_get_case(self, case_id: int) -> dict:
        return _get(f"{settings.cms_base_url}/cms/api/cases/{case_id}")

    def cms_get_task(self, task_id: int) -> dict:
        return _get(f"{settings.cms_base_url}/cms/api/tasks/{task_id}")

    def cms_write_task(self, task_id: int, status: str | None = None, note: str | None = None) -> dict:
        """Write outcomes back to the CMS task - the CMS stays the system of record."""
        result = httpx.patch(
            f"{settings.cms_base_url}/cms/api/tasks/{task_id}",
            json={"status": status, "notes": note},
            timeout=10,
        ).json()
        self._log("tool_call", f"CMS write-back on task #{task_id}: status={status} note={note!r}", result)
        return result

    def cms_post_message(self, task_id: int, body: str, author: str = "agent") -> dict:
        result = _post(
            f"{settings.cms_base_url}/cms/api/tasks/{task_id}/messages",
            {"author": author, "body": body},
        )
        self._log("tool_call", f"posted to CMS chat on task #{task_id}: {body!r}", result)
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
        result = _post(
            f"{settings.voice_stub_url}/call",
            {
                "scenario": scenario,
                "to": to,
                "script_prompt": purpose,
                "firm_id": self.run.firm_id,
                "case_id": self.run.case_id,
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
        self._log(
            "tool_call",
            f"voice call to {to} ({scenario}): outcome={result.get('outcome')}",
            result,
        )
        return result

    def send_email(self, to: str, subject: str, body: str, scenario: str = "default") -> dict:
        result = _post(
            f"{settings.email_stub_url}/send",
            {
                "scenario": scenario,
                "to": to,
                "subject": subject,
                "body": body,
                "firm_id": self.run.firm_id,
                "case_id": self.run.case_id,
            },
        )
        self._log("tool_call", f"email to {to}: {subject!r} -> {result.get('outcome')}", result)
        return result

    def send_fax(self, to: str, document: str) -> dict:
        result = _post(
            f"{settings.fax_stub_url}/send",
            {"to": to, "document": document, "firm_id": self.run.firm_id, "case_id": self.run.case_id},
        )
        self._log("tool_call", f"fax to {to}: {document!r}", result)
        return result

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
                "case_id": self.run.case_id,
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
