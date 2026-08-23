"""Stub communication channels: voice, email, fax.

Each call/email/fax is played against a scripted scenario so demos are
repeatable. Everything is logged and inspectable from the dashboard.

The voice stub doubles as the 'LLM': it returns a structured outcome
(answered / status / records_ready / refused / needs_payment ...) which the
agent reasons over. In production that structure would come from the model;
here it comes from the scenario script.
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


# ---------- voice ----------

class CallIn(BaseModel):
    scenario: str = "default"
    to: str = ""
    script_prompt: str = ""   # what the agent 'says' it wants
    firm_id: int = 0
    case_ref: str = ""


@router.post("/voice/call")
def make_call(body: CallIn):
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
