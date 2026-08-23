"""Seed the POC demo: one firm, one PI case, goals' triggers, scripted stubs.

Platform setup (the new architecture, as data):
  - a PlatformFirm bound to the stub-CMS connector,
  - per-firm agent configs: @records-agent (guardrail on, portal skill
    attached) and @checkin-agent (org-chart skill attached),
  - standing triggers: a staff chat message containing a handle opens a goal
    for that agent (replaces the old hardcoded webhook agent).

Scenarios:
  - "Metro General Hospital" releases records on the second portal check
    -> happy path: tag agent, it loads the portal skill, requests via portal,
       follows up by phone, records get released, task closes.
  - "County Records Bureau" demands payment on the first call
    -> escalation path (kind=task: a human must pay): answer in the dashboard,
       the CMS chat, or by simulated inbound email; agent resumes.
  - "Client wellness check-in" -> case-scoped long-horizon goal via
    @checkin-agent (org-chart skill visible in its context).

Run with the server up:  uv run python scripts/seed.py
"""

from __future__ import annotations

import json
import sqlite3
import sys

BASE = "http://localhost:8000"


def main() -> None:
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

    from app.config import settings
    from app.main import init_db  # creates tables if the server hasn't yet
    from app.platform.db import SessionLocal
    from app.platform.models import AgentConfig, PlatformFirm, Trigger
    from app.stubs.cms_models import Case, Contact, Firm, Task

    init_db()
    db = SessionLocal()

    if db.query(Firm).count():
        print("already seeded; skipping")
        return

    firm = Firm(name="Doe & Associates Injury Law")
    db.add(firm)
    db.flush()

    # platform-side firm binding (connector + per-firm agent configs) ---------
    pfirm = PlatformFirm(
        id=firm.id, name=firm.name, connector_key="stub_cms",
        cms_firm_ref=str(firm.id), config_json="{}",
    )
    db.add(pfirm)
    db.flush()

    records_cfg = AgentConfig(
        firm_id=pfirm.id, agent_name="medical-record-agent", handle="@records-agent",
        skills_json=json.dumps([
            "metro-general-portal",
            "stub-cms-basics",
            "trauma-records-request",
            "firms/doe-and-associates/org-chart",
        ]),
        guardrail_focus=(
            "Do not commit the firm to payments, settlement figures, or dates. "
            "Verify the provider/recipient matches the case contacts before any "
            "outbound call or email. Never share client medical details with a "
            "third party. Do not invent case facts. Judge actions against the "
            "tool's real arguments — do not demand identifiers the tool cannot "
            "take. Escalations and internal staff notes are low risk: allow them."
        ),
    )
    checkin_cfg = AgentConfig(
        firm_id=pfirm.id, agent_name="client-checkin-agent", handle="@checkin-agent",
        skills_json=json.dumps(["stub-cms-basics", "firms/doe-and-associates/org-chart"]),
        guardrail_focus=(
            "Client-facing: be warm, never clinical. Never promise settlement "
            "amounts or dates, never give legal advice. If the client asks for "
            "a human, redirect per the org chart (Sam Reyes), don't improvise."
        ),
        cadence_days=14.0,
    )
    db.add_all([records_cfg, checkin_cfg])
    db.flush()

    # standing triggers: staff tags an agent handle in the CMS task chat ------
    db.add_all([
        Trigger(firm_id=pfirm.id, agent_config_id=records_cfg.id,
                event_type="staff_message",
                match_json=json.dumps({"handle": "@records-agent"}), enabled=True),
        Trigger(firm_id=pfirm.id, agent_config_id=checkin_cfg.id,
                event_type="staff_message",
                match_json=json.dumps({"handle": "@checkin-agent"}), enabled=True),
    ])

    case = Case(
        firm_id=firm.id,
        case_number="PI-2026-0142",
        client_name="Maria Santos",
        summary="Rear-end collision on 2026-05-30. Client treated at Metro General Hospital. "
                "Demand not yet sent; waiting on complete medical records.",
    )
    db.add(case)
    db.flush()

    db.add_all([
        Contact(case_id=case.id, role="client", name="Maria Santos",
                phone="+1-555-0142", email="maria.santos@example.com",
                details="Rear-ended at a stoplight. Lower back injury, PT twice a week."),
        Contact(case_id=case.id, role="hospital", name="Metro General Hospital",
                phone="+1-555-0900", email="records@metrogeneral.example.com",
                fax="+1-555-0901",
                details="Records released via their provider portal. Sometimes asks for payment first."),
        Contact(case_id=case.id, role="provider", name="County Records Bureau",
                phone="+1-555-0777", email="requests@countyrecords.example.com",
                details="Third-party records provider. Requires prepayment."),
    ])

    t1 = Task(case_id=case.id,
              title="Get medical records from Metro General Hospital (ER visit 2026-05-30)")
    t2 = Task(case_id=case.id,
              title="Get billing records from County Records Bureau")
    t3 = Task(case_id=case.id,
              title="Client wellness check-in — Maria Santos")
    db.add_all([t1, t2, t3])
    db.commit()

    # scripted stub behaviour -------------------------------------------------
    db_path = settings.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "INSERT OR REPLACE INTO stub_scenarios (name, script, step) VALUES (?, ?, 0)",
            [
                # portal: second check shows the release
                ("portal:+1-555-0900", json.dumps([
                    {"portal_status": "processing"},
                    {"portal_status": "released"},
                    {"portal_status": "released"},
                ])),
                # calls to the hospital: processing, then ready
                ("provider:+1-555-0900", json.dumps([
                    {"outcome": "answered", "spoken": "Yes, we have the request, it is being processed."},
                    {"outcome": "records_ready", "spoken": "The records were released on the portal today."},
                    {"outcome": "records_ready", "spoken": "The records were released already."},
                ])),
                # the bureau always demands payment first
                ("provider:+1-555-0777", json.dumps([
                    {"outcome": "needs_payment", "spoken": "We need the $45 invoice paid before we release anything."},
                    {"outcome": "needs_payment", "spoken": "Still waiting on that invoice payment."},
                ])),
                ("portal:+1-555-0777", json.dumps([
                    {"portal_status": "awaiting_payment"},
                    {"portal_status": "released"},   # released once the invoice is paid
                ])),
                ("default", json.dumps([
                    {"outcome": "no_answer", "spoken": ""},
                ])),
                # client check-in: Maria answers the phone, shares an update + concern
                ("provider:+1-555-0142", json.dumps([
                    {"outcome": "answered", "spoken": "Hi, I'm doing okay. PT is helping a little, but my back still hurts at night."},
                    {"outcome": "answered", "spoken": "I'm worried because I still can't work, and I haven't heard anything about my case."},
                    {"outcome": "no_answer", "spoken": ""},
                ])),
                # client check-in over email (fallback channel)
                ("email:maria.santos@example.com", json.dumps([
                    {"outcome": "sent", "spoken": "Hi, doing okay. PT twice a week. When will my case settle?"},
                    {"outcome": "sent", "spoken": "No change, still waiting to hear about my case."},
                ])),
            ],
        )
        conn.commit()

    print(f"seeded firm #{firm.id} '{firm.name}', case #{case.id} '{case.case_number}'")
    print(f"  platform firm #{pfirm.id} bound to connector 'stub_cms'")
    print(f"  agent configs: #{records_cfg.id} @records-agent (guardrail on, 4 skills), "
          f"#{checkin_cfg.id} @checkin-agent (guardrail on, 2 skills)")
    print(f"  task #{t1.id}: {t1.title}   (happy path)")
    print(f"  task #{t2.id}: {t2.title}   (escalation path)")
    print(f"  task #{t3.id}: {t3.title}   (client check-in)")
    print()
    print("try:")
    print(f"  uv run python -c \"import httpx; httpx.post('{BASE}/cms/api/tasks/{t1.id}/messages', "
          f"json={{'author':'staff','body':'@records-agent please follow up on this'}})\"")
    print("  then watch http://localhost:8000/ and http://localhost:8000/cms/board")


if __name__ == "__main__":
    main()
