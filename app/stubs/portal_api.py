"""Stub provider records portal.

Stands in for a medical-records provider's web app. The agent's portal tool
submits a records request here (the 'browser automation' path) and checks its
status. Releases are scripted per provider phone number so the demo is
repeatable; when MISTRAL_API_KEY is set, the portal can live-generate the
status so the demo feels like real progress.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.config import settings

router = APIRouter(prefix="/stubs/portal", tags=["portal"])


def _conn() -> sqlite3.Connection:
    path = settings.database_url.removeprefix("sqlite:///")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn


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


def _live_portal_status(provider_key: str, client_name: str, case_number: str) -> str | None:
    """Ask Mistral what the portal status should be for this request."""
    if not settings.mistral_api_key:
        return None
    try:
        from app.platform.llm import chat
        system = """You are the backend of a medical-records provider portal.
Generate the next realistic status for a records request. Return ONLY raw JSON (no markdown fences) with a single field:
  portal_status: one of submitted | processing | awaiting_payment | released
Providers typically start at submitted/processing and move to released after a call or a day.
If the request has already been open for a while, move it toward released.
Be realistic: hospitals eventually release, third-party providers require payment first, fax-only clinics require a faxed HIPAA form."""
        user = f"Provider: {provider_key}\nClient: {client_name}\nCase: {case_number}\nWhat is the current portal status?"
        msg = chat([
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ])
        return _parse_llm_json(msg.content).get("portal_status")
    except Exception:
        return None


class RecordsRequestIn(BaseModel):
    provider_key: str          # stable key for the provider (we use phone number)
    client_name: str
    case_number: str
    hipaa_on_file: bool = True
    firm_id: int = 0
    case_ref: str = ""


@router.post("/requests")
def submit_request(body: RecordsRequestIn):
    now = datetime.now(timezone.utc).isoformat()
    with _conn() as conn:
        existing = conn.execute(
            "SELECT id FROM portal_requests WHERE provider_key = ? AND case_number = ? AND status != 'cancelled'",
            (body.provider_key, body.case_number),
        ).fetchone()
        if existing:
            return {"request_id": existing["id"], "status": "already_open"}
        cur = conn.execute(
            """INSERT INTO portal_requests
               (provider_key, client_name, case_number, hipaa_on_file, status, firm_id, case_ref, created_at)
               VALUES (?, ?, ?, ?, 'submitted', ?, ?, ?)""",
            (body.provider_key, body.client_name, body.case_number, int(body.hipaa_on_file),
             body.firm_id, body.case_ref, now),
        )
        conn.commit()
        return {"request_id": cur.lastrowid, "status": "submitted"}


@router.get("/requests/{request_id}")
def check_request(request_id: int):
    """Advance the release schedule for this provider on each check.

    When MISTRAL_API_KEY is set, ask the LLM for the next status so the demo
    feels live; otherwise fall back to the static scenario script.
    """
    with _conn() as conn:
        row = conn.execute("SELECT * FROM portal_requests WHERE id = ?", (request_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "request not found")
        status = row["status"]
        if status not in ("released",):
            live = _live_portal_status(row["provider_key"], row["client_name"], row["case_number"])
            if live is not None:
                status = live
            else:
                scenario = conn.execute(
                    "SELECT script, step FROM stub_scenarios WHERE name = ?", (f"portal:{row['provider_key']}",)
                ).fetchone()
                if scenario is not None:
                    script = json.loads(scenario["script"])
                    step = scenario["step"]
                    turn = script[min(step, len(script) - 1)]
                    status = turn.get("portal_status", status)
                    if step < len(script) - 1:
                        conn.execute(
                            "UPDATE stub_scenarios SET step = ? WHERE name = ?",
                            (step + 1, f"portal:{row['provider_key']}"),
                        )
            if status != row["status"]:
                conn.execute(
                    "UPDATE portal_requests SET status = ? WHERE id = ?", (status, request_id)
                )
            conn.commit()
        return {
            "request_id": row["id"],
            "provider_key": row["provider_key"],
            "case_number": row["case_number"],
            "status": status,
        }
