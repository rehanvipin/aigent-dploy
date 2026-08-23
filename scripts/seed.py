"""Seed the POC demo: one firm, one PI case, a records task, scripted stubs.

Scenarios:
  - "Metro General Hospital" releases records on the second portal check
    -> happy path: tag agent, it requests via portal, follows up by phone,
       records get released, task closes.
  - "County Records Bureau" demands payment on the first call
    -> escalation path: agent parks, staff answers in dashboard or CMS chat,
       agent resumes.

Run with the server up:  uv run python scripts/seed.py
"""

from __future__ import annotations

import json
import sqlite3
import sys

import httpx

BASE = "http://localhost:8000"


def main() -> None:
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

    from app.config import settings
    from app.main import init_db  # creates tables if the server hasn't yet
    from app.platform.db import SessionLocal
    from app.stubs.cms_models import Case, Contact, Firm, Task

    init_db()
    db = SessionLocal()

    if db.query(Firm).count():
        print("already seeded; skipping")
        return

    firm = Firm(name="Doe & Associates Injury Law")
    db.add(firm)
    db.flush()

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
    print(f"  task #{t1.id}: {t1.title}   (happy path)")
    print(f"  task #{t2.id}: {t2.title}   (escalation path)")
    print(f"  task #{t3.id}: {t3.title}   (client check-in)")
    print()
    print("try:")
    print(f"  curl -s -X POST {BASE}/cms/api/tasks/{t1.id}/messages \\")
    print('       -H \'content-type: application/json\' \\')
    print('       -d \'{"author":"staff","body":"@records-agent please follow up on this"}\'')
    print("  then watch http://localhost:8000/ and http://localhost:8000/cms/board")


if __name__ == "__main__":
    main()
