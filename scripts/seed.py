"""Seed the platform with rich, realistic demo data: 2 firms, 10 PI cases,
3 providers, 10 check-in clients.

Creates:
  - 2 CMS firms (stub CMS side)
  - 2 platform firms (platform side, linked via cms_firm_ref)
  - 4 agent configs (2 per firm: @records-agent + @checkin-agent)
  - 4 standing triggers (2 per firm, matching each handle)
  - 10 CMS cases with contacts and tasks
  - Goals and agent runs for each task (short-horizon for records,
    long-horizon for check-ins)
  - Real agent-executed runs and communications for a representative subset
    when MISTRAL_API_KEY is present
  - Chat threads with staff messages on every task
  - Stub scenarios: 3 providers (Metro General happy path, County Records payment
    escalation, Brightway Orthopedic fax-first), 10 client check-in scripts,
    staff/provider email scripts.

Run with the server up:  uv run python scripts/seed.py
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import httpx

BASE = "http://localhost:8000"


# ---------------------------------------------------------------------------
# Data definitions
# ---------------------------------------------------------------------------

FIRMS = [
    {
        "name": "Doe & Associates Injury Law",
        "slug": "doe-and-associates",
        "skills": ["metro-general-portal", "stub-cms-basics", "trauma-records-request",
                    "firms/doe-and-associates/org-chart"],
        "agents": [
            {
                "agent_name": "medical-record-agent",
                "handle": "@records-agent",
                "guardrail_focus": "Never invent facts or amounts. Never promise a specific settlement figure. Escalate if the provider refuses or demands payment.",
                "cadence_days": 7.0,
            },
            {
                "agent_name": "client-checkin-agent",
                "handle": "@checkin-agent",
                "guardrail_focus": "Never promise a specific settlement figure or timeline. Never give medical advice. Create a staff task if the client expresses a concern or requests action.",
                "cadence_days": 14.0,
            },
        ],
    },
    {
        "name": "Marchetti & Voss Injury Attorneys",
        "slug": "marchetti-voss",
        "skills": ["stub-cms-basics", "firms/marchetti-voss/org-chart"],
        "agents": [
            {
                "agent_name": "medical-record-agent",
                "handle": "@records-agent",
                "guardrail_focus": "Never invent facts or amounts. Never promise a specific settlement figure. Escalate if the provider refuses or demands payment.",
                "cadence_days": 7.0,
            },
            {
                "agent_name": "client-checkin-agent",
                "handle": "@checkin-agent",
                "guardrail_focus": "Never promise a specific settlement figure or timeline. Never give medical advice. Create a staff task if the client expresses a concern or requests action.",
                "cadence_days": 14.0,
            },
        ],
    },
]

# (firm_name, case_number, client_name, client_phone, client_email, summary, tasks, contacts)
CASES = [
    # ── Firm 1 (Doe & Associates) ──────────────────────────────────────────
    {
        "firm": "Doe & Associates Injury Law",
        "case_number": "PI-2026-0142",
        "client_name": "Maria Santos",
        "client_phone": "+1-555-0142",
        "client_email": "maria.santos@example.com",
        "summary": "Rear-end collision on 2026-05-30. Client treated at Metro General Hospital. Demand not yet sent; waiting on complete medical records.",
        "tasks": [
            "Get medical records from Metro General Hospital (ER visit 2026-05-30)",
            "Get billing records from County Records Bureau",
            "Client wellness check-in — Maria Santos",
        ],
        "contacts": [
            {"role": "hospital", "name": "Metro General Hospital", "phone": "+1-555-0900",
             "email": "records@metrogeneral.example.com", "fax": "+1-555-0901",
             "details": "Records released via their provider portal. Sometimes asks for payment first."},
            {"role": "provider", "name": "County Records Bureau", "phone": "+1-555-0777",
             "email": "requests@countyrecords.example.com",
             "details": "Third-party records provider. Requires prepayment."},
        ],
        "staff_messages": [
            {"author": "Paralegal — Nadia", "body": "Intake notes: Maria rear-ended at a stoplight. Lower back pain, starting PT 2x/week. Clear liability — police report confirms other driver ran red."},
            {"author": "Atty R. Doe", "body": "Let's prioritize the Metro General records — we need the ER chart before we can build the demand. County Records billing is secondary."},
        ],
        "records_tasks": [
            {"task_index": 0, "provider_key": "+1-555-0900", "provider_name": "Metro General Hospital"},
            {"task_index": 1, "provider_key": "+1-555-0777", "provider_name": "County Records Bureau"},
        ],
    },
    {
        "firm": "Doe & Associates Injury Law",
        "case_number": "PI-2026-0155",
        "client_name": "James Whitfield",
        "client_phone": "+1-555-0155",
        "client_email": "james.whitfield@example.com",
        "summary": "Trip and fall on a broken city sidewalk on 2026-04-12. Client tripped over a raised concrete slab, fractured left hip. Surgery with ORIF, inpatient rehab. City is responsible for sidewalk maintenance.",
        "tasks": [
            "Client wellness check-in — James Whitfield",
        ],
        "contacts": [
            {"role": "client", "name": "James Whitfield", "phone": "+1-555-0155",
             "email": "james.whitfield@example.com",
             "details": "68-year-old retired teacher. Hip fracture with surgical repair, rehab ongoing. Limited mobility."},
        ],
        "staff_messages": [
            {"author": "Paralegal — Nadia", "body": "Intake: James Whitfield, 68, tripped on a broken sidewalk on Oak Street. City had received complaints about this slab 3 months prior — building our notice argument."},
            {"author": "Sam Reyes", "body": "City has 90-day notice period for sovereign immunity. Calendar the deadline: 2026-07-12."},
        ],
    },
    {
        "firm": "Doe & Associates Injury Law",
        "case_number": "PI-2026-0163",
        "client_name": "Linda Chen",
        "client_phone": "+1-555-0163",
        "client_email": "linda.chen@example.com",
        "summary": "Slip and fall at FreshMart grocery store on 2026-03-28. Client slipped on an unmarked wet floor in the produce section, sustained a distal radius fracture. Security camera footage confirms spill existed 18+ minutes before the fall.",
        "tasks": [
            "Client wellness check-in — Linda Chen",
        ],
        "contacts": [
            {"role": "client", "name": "Linda Chen", "phone": "+1-555-0163",
             "email": "linda.chen@example.com",
             "details": "34-year-old office worker. Wrist fracture, cast for 6 weeks, then PT. Strong case — video evidence of negligence."},
        ],
        "staff_messages": [
            {"author": "Paralegal — Nadia", "body": "Linda Chen, FreshMart slip and fall. We have the security footage — spill existed for 18 min before her fall, no wet floor sign. Strong negligence case."},
        ],
    },
    {
        "firm": "Doe & Associates Injury Law",
        "case_number": "PI-2026-0178",
        "client_name": "David Okafor",
        "client_phone": "+1-555-0178",
        "client_email": "david.okafor@example.com",
        "summary": "Injured by a county transit bus on 2026-02-15. Client was a passenger when the bus made an abrupt stop, causing a fall. Herniated disc (L4-L5) requiring epidural injections and pain management. County transit authority is the defendant.",
        "tasks": [
            "Client wellness check-in — David Okafor",
        ],
        "contacts": [
            {"role": "client", "name": "David Okafor", "phone": "+1-555-0178",
             "email": "david.okafor@example.com",
             "details": "42-year-old accountant. Herniated disc, ongoing pain management, limited sitting tolerance affects work."},
        ],
        "staff_messages": [
            {"author": "Paralegal — Nadia", "body": "David Okafor, county transit bus injury. Bus driver made abrupt stop, David fell. L4-L5 herniated disc confirmed by MRI. County transit authority is the defendant — sovereign immunity claim with 180-day notice already filed."},
            {"author": "Atty R. Doe", "body": "We have the incident report from the bus. Driver admitted to a 'sudden brake' for a pedestrian. David was seated, no handrail nearby."},
        ],
    },
    {
        "firm": "Doe & Associates Injury Law",
        "case_number": "PI-2026-0187",
        "client_name": "Rachel Foster",
        "client_phone": "+1-555-0187",
        "client_email": "rachel.foster@example.com",
        "summary": "Pharmacy mis-dispensed medication on 2026-01-20. Client was given the wrong dosage of a pain medication, causing an adverse reaction requiring ER visit. Pharmacy error confirmed by medication reconciliation.",
        "tasks": [
            "Get medical records from Brightway Orthopedic Clinic (post-ER follow-up)",
            "Client wellness check-in — Rachel Foster",
        ],
        "contacts": [
            {"role": "hospital", "name": "Brightway Orthopedic Clinic", "phone": "+1-555-0912",
             "email": "records@brightwayclinic.example.com",
             "details": "Small private orthopedic clinic. Fax-only for records requests — requires signed HIPAA authorization by fax before releasing on their portal."},
            {"role": "adjuster", "name": "Statewide Insurance — Adjuster Mark Webb", "phone": "+1-555-0299",
             "email": "mwebb@statewide.example.com",
             "details": "Adjuster handling the pharmacy malpractice claim. Has requested independent medical examination (IME)."},
        ],
        "staff_messages": [
            {"author": "Paralegal — Nadia", "body": "Rachel Foster — pharmacy dispensed wrong dosage of oxycodone. ER visit for adverse reaction. Need Brightway Orthopedic records (they treated her post-ER) and pharmacy records."},
            {"author": "Sam Reyes", "body": "Mark Webb at Statewide wants an IME. We have 30 days to respond. Hold off on the demand until we get the Brightway records."},
        ],
        "records_tasks": [
            {"task_index": 0, "provider_key": "+1-555-0912", "provider_name": "Brightway Orthopedic Clinic"},
        ],
    },
    {
        "firm": "Doe & Associates Injury Law",
        "case_number": "PI-2026-0199",
        "client_name": "Thomas Nguyen",
        "client_phone": "+1-555-0199",
        "client_email": "thomas.nguyen@example.com",
        "summary": "Pedestrian hit by a delivery bike courier on 2026-06-05. Client was crossing at a crosswalk when a delivery courier on an e-bike ran the red light. Concussion with persistent headaches, soft tissue injuries. Courier was on active delivery for QuickMart.",
        "tasks": [
            "Client wellness check-in — Thomas Nguyen",
        ],
        "contacts": [
            {"role": "client", "name": "Thomas Nguyen", "phone": "+1-555-0199",
             "email": "thomas.nguyen@example.com",
             "details": "29-year-old software engineer. Concussion with post-concussion syndrome, persistent headaches, neck strain. Cognitive testing scheduled."},
        ],
        "staff_messages": [
            {"author": "Paralegal — Nadia", "body": "Thomas Nguyen, pedestrian hit by QuickMart delivery courier. Courier ran red light — traffic camera confirms. Concussion with ongoing symptoms. We have the courier's employment records showing he was on active delivery."},
        ],
    },
    # ── Firm 2 (Marchetti & Voss) ──────────────────────────────────────────
    {
        "firm": "Marchetti & Voss Injury Attorneys",
        "case_number": "PI-2026-0201",
        "client_name": "Elena Petrov",
        "client_phone": "+1-555-0201",
        "client_email": "elena.petrov@example.com",
        "summary": "Dog bite by neighbor's dog on 2026-05-10. Client was walking past a neighbor's property when the dog, which had a known history of aggression, escaped through an unsecured gate. Client suffered facial lacerations requiring reconstructive surgery.",
        "tasks": [
            "Client wellness check-in — Elena Petrov",
        ],
        "contacts": [
            {"role": "client", "name": "Elena Petrov", "phone": "+1-555-0201",
             "email": "elena.petrov@example.com",
             "details": "26-year-old teacher. Facial lacerations, reconstructive surgery, scar management. Dog had 2 prior bite complaints with animal control."},
        ],
        "staff_messages": [
            {"author": "Paralegal — Rosa", "body": "Elena Petrov, dog bite case. Neighbor's dog escaped through unsecured gate. Dog has 2 prior bite complaints with animal control — this is key for negligence per se. Facial lacerations, plastic surgery consultation scheduled."},
        ],
    },
    {
        "firm": "Marchetti & Voss Injury Attorneys",
        "case_number": "PI-2026-0215",
        "client_name": "Marcus Williams",
        "client_phone": "+1-555-0215",
        "client_email": "marcus.williams@example.com",
        "summary": "Elevator malfunction in a public parking garage on 2026-04-22. Client was exiting the elevator when the brake failed, causing a 4-foot drop. Client sustained a spinal compression injury (T12) with partial paralysis below the waist. Garage owned by City Parking Authority.",
        "tasks": [
            "Client wellness check-in — Marcus Williams",
        ],
        "contacts": [
            {"role": "client", "name": "Marcus Williams", "phone": "+1-555-0215",
             "email": "marcus.williams@example.com",
             "details": "38-year-old warehouse supervisor. Spinal compression injury, incomplete paralysis, wheelchair-bound pending surgery. Career-ending injury for physical labor."},
        ],
        "staff_messages": [
            {"author": "David Voss", "body": "Marcus Williams case — elevator brake failure at City Parking Authority garage. This is a sovereign entity — we filed notice within 90 days. Last inspection was 14 months ago; code requires 6-month inspections."},
            {"author": "Paralegal — Rosa", "body": "We have the elevator maintenance log and the city's inspection schedule. Gap in inspection is our strongest argument for negligence."},
        ],
    },
    {
        "firm": "Marchetti & Voss Injury Attorneys",
        "case_number": "PI-2026-0228",
        "client_name": "Aiden Brooks",
        "client_phone": "+1-555-0228",
        "client_email": "sarah.brooks@example.com",
        "summary": "Child injured on a school playground on 2026-03-14. 8-year-old client fell from a defective climbing structure that had missing safety bolts. Femur fracture requiring surgical repair with intramedullary nailing. School district is responsible for playground maintenance.",
        "tasks": [
            "Client wellness check-in — Aiden Brooks",
        ],
        "contacts": [
            {"role": "client", "name": "Sarah Brooks (parent of Aiden Brooks)", "phone": "+1-555-0228",
             "email": "sarah.brooks@example.com",
             "details": "8-year-old boy. Femur fracture from defective playground equipment. Surgical repair, recovery ongoing. School had prior complaints about the climbing structure."},
        ],
        "staff_messages": [
            {"author": "Carla Marchetti", "body": "Aiden Brooks — school playground injury. The climbing structure had missing safety bolts; school had 2 prior maintenance requests about this equipment. We need to preserve the equipment as evidence before they repair it."},
            {"author": "Paralegal — Rosa", "body": "Parent (Sarah Brooks) is very anxious. Call her back today with a status update. She wants to know about potential settlement timeline — avoid giving specific numbers."},
        ],
    },
    {
        "firm": "Marchetti & Voss Injury Attorneys",
        "case_number": "PI-2026-0234",
        "client_name": "Diane Kowalski",
        "client_phone": "+1-555-0234",
        "client_email": "diane.kowalski@example.com",
        "summary": "Broken ankle on an unsalted county walkway on 2026-01-08. Client slipped on ice on a county-maintained walkway near the civic center. Ankle ORIF surgery with hardware. County had received a salt-application complaint 2 days prior.",
        "tasks": [
            "Client wellness check-in — Diane Kowalski",
        ],
        "contacts": [
            {"role": "client", "name": "Diane Kowalski", "phone": "+1-555-0234",
             "email": "diane.kowalski@example.com",
             "details": "52-year-old nurse. Ankle ORIF with plate and screws, non-weight-bearing for 6 weeks. Work as a nurse requires standing/walking — significant impact on livelihood."},
        ],
        "staff_messages": [
            {"author": "David Voss", "body": "Diane Kowalski — county walkway ice injury. County received a salt complaint 2 days before the fall. We have the complaint log via public records request. Diane is a nurse — standing/walking is essential for her job, which strengthens the lost-wage claim."},
        ],
    },
]

# Medical records providers (3 total)
PROVIDERS = {
    "+1-555-0900": {
        "name": "Metro General Hospital",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "Yes, we have the request on file. It is being processed — typically 5 to 7 business days."},
            {"outcome": "records_ready", "spoken": "Good news — the records were released on the portal today. You should be able to download them now."},
            {"outcome": "records_ready", "spoken": "The records are already released. Check the portal."},
        ],
        "portal_scenario": [
            {"portal_status": "processing"},
            {"portal_status": "released"},
            {"portal_status": "released"},
        ],
    },
    "+1-555-0777": {
        "name": "County Records Bureau",
        "voice_scenario": [
            {"outcome": "needs_payment", "spoken": "We have the request, but we need the $45 invoice paid before we can release anything."},
            {"outcome": "needs_payment", "spoken": "Still waiting on that invoice payment. We cannot release until it is paid."},
        ],
        "portal_scenario": [
            {"portal_status": "awaiting_payment"},
            {"portal_status": "released"},
        ],
    },
    "+1-555-0912": {
        "name": "Brightway Orthopedic Clinic",
        "voice_scenario": [
            {"outcome": "needs_fax", "spoken": "We only accept signed HIPAA authorizations by fax. Please fax the signed form to us at +1-555-0912 and we will release the records on our portal."},
            {"outcome": "needs_fax", "spoken": "We still need that faxed authorization. Once we receive it, the records will be released within 48 hours."},
        ],
        "portal_scenario": [
            {"portal_status": "awaiting_fax"},
            {"portal_status": "processing"},
            {"portal_status": "released"},
        ],
    },
}

# Client check-in scenarios (10 clients, voice + email fallback)
CLIENT_CHECKINS = [
    {
        "name": "Maria Santos",
        "phone": "+1-555-0142",
        "email": "maria.santos@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "Hi, I'm doing okay. PT is helping a little, but my back still hurts at night and I can't sleep well."},
            {"outcome": "answered", "spoken": "I'm worried because I still can't work, and I haven't heard anything about my case. When will my case settle?"},
            {"outcome": "no_answer", "spoken": ""},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Hi, doing okay. PT twice a week. When will my case settle? I'm getting anxious about the timeline."},
            {"outcome": "sent", "spoken": "No change, still waiting to hear about my case. My back pain is a bit better but I still can't lift heavy things."},
        ],
    },
    {
        "name": "James Whitfield",
        "phone": "+1-555-0155",
        "email": "james.whitfield@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "I'm getting better slowly. The hip is healing, but I still need a cane to walk. Physical therapy is three times a week."},
            {"outcome": "answered", "spoken": "My wife has been doing most of the yard work. I'm frustrated — I used to be so active. Is there any update on the city claim?"},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Hip is progressing. Still using a cane. Any news on the city claim?"},
        ],
    },
    {
        "name": "Linda Chen",
        "phone": "+1-555-0163",
        "email": "linda.chen@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "The cast is off now! I'm in a splint and starting PT. My wrist is stiff but the doctor says it's healing well."},
            {"outcome": "answered", "spoken": "I'm worried about scarring. The doctor said there might be some permanent marks. Will that affect my case?"},
            {"outcome": "no_answer", "spoken": ""},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Cast is off, starting PT. Wrist is stiff but healing. Any update on the FreshMart claim?"},
        ],
    },
    {
        "name": "David Okafor",
        "phone": "+1-555-0178",
        "email": "david.okafor@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "The pain management is helping some days, but other days the sciatica is terrible. I can't sit at my desk for more than an hour."},
            {"outcome": "answered", "spoken": "My employer is pressuring me to come back full-time. I'm only cleared for light duty. This is really stressful."},
            {"outcome": "answered", "spoken": "I had another epidural injection last week. Doctor says we might need to consider surgery if this doesn't help."},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Pain management ongoing. Another epidural last week. My employer is pressuring me to return full-time but I'm only cleared for light duty."},
        ],
    },
    {
        "name": "Rachel Foster",
        "phone": "+1-555-0187",
        "email": "rachel.foster@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "I'm feeling better after the ER visit, but I'm still shaken up. The wrong medication could have been much worse."},
            {"outcome": "answered", "spoken": "I've been having headaches since the incident. My doctor is monitoring me. Is the pharmacy cooperating with the investigation?"},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Feeling better but still shaken. Headaches since the incident. Any update on the pharmacy investigation?"},
        ],
    },
    {
        "name": "Thomas Nguyen",
        "phone": "+1-555-0199",
        "email": "thomas.nguyen@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "The headaches are getting better but I still have brain fog. I can't focus on code for more than a few hours. My employer is being understanding but I'm worried about my performance reviews."},
            {"outcome": "no_answer", "spoken": ""},
            {"outcome": "answered", "spoken": "I had another neurology appointment. The doctor says post-concussion syndrome can take months to fully resolve. I'm doing cognitive therapy twice a week."},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Headaches improving but still have brain fog. Can't focus for long periods. Cognitive therapy 2x/week. Any update on the QuickMart claim?"},
        ],
    },
    {
        "name": "Elena Petrov",
        "phone": "+1-555-0201",
        "email": "elena.petrov@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "The scars are healing but my dermatologist says I'll need laser treatment. I'm self-conscious about the marks on my face."},
            {"outcome": "answered", "spoken": "I had to take a leave of absence from teaching because the kids were staring. I'm going back next week but I'm nervous."},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Scars healing but need laser treatment. Going back to teaching next week. Any news on the neighbor's dog case?"},
        ],
    },
    {
        "name": "Marcus Williams",
        "phone": "+1-555-0215",
        "email": "marcus.williams@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "I'm in a wheelchair still. Surgery is scheduled for next month. The doctors are hopeful I'll regain some function but it's going to be a long road."},
            {"outcome": "answered", "spoken": "My wife has been incredible but this is hard. I can't do anything around the house. I'm worried about money — I was the primary earner."},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Still wheelchair-bound. Surgery next month. Worried about finances — I was the primary earner. Any update on the case?"},
        ],
    },
    {
        "name": "Sarah Brooks (parent of Aiden Brooks)",
        "phone": "+1-555-0228",
        "email": "sarah.brooks@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "Aiden is doing much better. He's back at school but can't do PE or recess for another month. He's asking a lot of questions about what happened."},
            {"outcome": "answered", "spoken": "We're still getting bills from the surgery. The school's insurance company sent us a letter — should I forward that to you?"},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Aiden is back at school, no PE for a month. Getting bills from surgery. School's insurance sent a letter — should I forward it?"},
        ],
    },
    {
        "name": "Diane Kowalski",
        "phone": "+1-555-0234",
        "email": "diane.kowalski@example.com",
        "voice_scenario": [
            {"outcome": "answered", "spoken": "I'm non-weight-bearing for another two weeks. My employer is holding my position but I'm using up all my sick leave. This is really stressful financially."},
            {"outcome": "answered", "spoken": "The hardware in my ankle is causing discomfort. My orthopedic surgeon says I might need a second surgery to remove it after the bone heals."},
        ],
        "email_scenario": [
            {"outcome": "sent", "spoken": "Non-weight-bearing for 2 more weeks. Using sick leave, stressed about finances. Might need second surgery to remove hardware."},
        ],
    },
]

# Staff/provider email scenarios
EMAIL_SCENARIOS = {
    "email:intake@doe-law.example.com": [
        {"outcome": "sent", "spoken": "Thank you for contacting Doe & Associates. We have received your inquiry and a team member will follow up within 24 hours."},
    ],
    "email:intake@marchetti-voss.example.com": [
        {"outcome": "sent", "spoken": "Thank you for contacting Marchetti & Voss. We have received your inquiry and will be in touch shortly."},
    ],
    "email:records@brightwayclinic.example.com": [
        {"outcome": "sent", "spoken": "We only accept records requests by fax. Please fax a signed HIPAA authorization to +1-555-0912. We do not accept email requests for medical records."},
    ],
}


# ---------------------------------------------------------------------------
# Seed logic
# ---------------------------------------------------------------------------

def main() -> None:
    import pathlib
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

    from app.config import settings
    from app.main import init_db
    from app.platform.db import SessionLocal
    from app.platform.models import (
        AgentConfig,
        AgentRun,
        Communication,
        Escalation,
        EscalationKind,
        EscalationStatus,
        Goal,
        GoalHorizon,
        GoalStatus,
        PlatformFirm,
        RunEvent,
        RunStatus,
        Trigger,
    )
    from app.stubs.cms_models import Case, ChatMessage, ChatThread, Contact, Firm, Task

    init_db()
    db = SessionLocal()

    if db.query(PlatformFirm).count():
        print("already seeded; skipping")
        return

    # ── 1. Create CMS firms ────────────────────────────────────────────────
    cms_firms: dict[str, Firm] = {}
    for f in FIRMS:
        firm = Firm(name=f["name"])
        db.add(firm)
        db.flush()
        cms_firms[f["name"]] = firm

    # ── 2. Create platform firms ───────────────────────────────────────────
    platform_firms: dict[str, PlatformFirm] = {}
    for f in FIRMS:
        pf = PlatformFirm(
            name=f["name"],
            connector_key="stub_cms",
            cms_firm_ref=str(cms_firms[f["name"]].id),
            config_json="{}",
        )
        db.add(pf)
        db.flush()
        platform_firms[f["name"]] = pf

    # ── 3. Create agent configs + standing triggers ────────────────────────
    agent_configs: dict[str, AgentConfig] = {}  # key: "{firm_slug}:{agent_name}"
    for f in FIRMS:
        pf = platform_firms[f["name"]]
        for agent in f["agents"]:
            ac = AgentConfig(
                firm_id=pf.id,
                agent_name=agent["agent_name"],
                handle=agent["handle"],
                skills_json=json.dumps(f["skills"]),
                guardrail_focus=agent["guardrail_focus"],
                cadence_days=agent["cadence_days"],
                enabled=True,
            )
            db.add(ac)
            db.flush()
            key = f"{f['slug']}:{agent['agent_name']}"
            agent_configs[key] = ac

            trigger = Trigger(
                firm_id=pf.id,
                agent_config_id=ac.id,
                event_type="staff_message",
                match_json=json.dumps({"handle": agent["handle"]}),
                enabled=True,
            )
            db.add(trigger)

    # ── 4. Create CMS cases, contacts, tasks, chat threads ──────────
    # We collect task metadata here and open goals/runs in a later step so
    # the platform runtime (not hand-seeded rows) owns the agent lifecycle.
    task_infos: list[dict] = []
    for case_def in CASES:
        firm_name = case_def["firm"]
        cms_firm = cms_firms[firm_name]
        pf = platform_firms[firm_name]

        case = Case(
            firm_id=cms_firm.id,
            case_number=case_def["case_number"],
            client_name=case_def["client_name"],
            case_type="personal_injury",
            status="open",
            summary=case_def["summary"],
        )
        db.add(case)
        db.flush()

        # Client contact
        db.add(Contact(
            case_id=case.id,
            role="client",
            name=case_def["client_name"],
            phone=case_def["client_phone"],
            email=case_def["client_email"],
            details=next((c.get("details", "") for c in case_def.get("contacts", [])
                          if c.get("role") == "client"), ""),
        ))

        # Other contacts (skip client role — already created above)
        for c in case_def.get("contacts", []):
            if c.get("role") == "client":
                continue
            db.add(Contact(
                case_id=case.id,
                role=c.get("role", "provider"),
                name=c.get("name", ""),
                phone=c.get("phone", ""),
                email=c.get("email", ""),
                fax=c.get("fax", ""),
                details=c.get("details", ""),
            ))

        # Tasks
        task_objects: list[Task] = []
        for title in case_def["tasks"]:
            task = Task(case_id=case.id, title=title, status="open")
            db.add(task)
            db.flush()
            task_objects.append(task)

        # Collect task metadata for goal/run creation
        for i, task in enumerate(task_objects):
            is_records = any(r["task_index"] == i for r in case_def.get("records_tasks", []))
            if i == len(task_objects) - 1 and not is_records:
                # Last task is typically the check-in (if no explicit records_tasks match)
                agent_key = f"{FIRMS[FIRMS.index(next(f2 for f2 in FIRMS if f2['name'] == firm_name))]['slug']}:client-checkin-agent"
                horizon = GoalHorizon.LONG
                brief = f"Periodic wellness check-in with {case_def['client_name']} until case closes"
            else:
                slug = next(f2["slug"] for f2 in FIRMS if f2["name"] == firm_name)
                agent_key = f"{slug}:medical-record-agent"
                horizon = GoalHorizon.SHORT
                brief = task.title

            task_infos.append({
                "firm_id": pf.id,
                "case": case,
                "task": task,
                "agent_config_id": agent_configs[agent_key].id,
                "brief": brief,
                "horizon": horizon,
                "firm_name": firm_name,
                "case_number": case_def["case_number"],
                "task_index": i,
            })

        # Staff chat messages on the first task
        if task_objects and case_def.get("staff_messages"):
            thread = ChatThread(task_id=task_objects[0].id)
            db.add(thread)
            db.flush()
            for msg in case_def["staff_messages"]:
                db.add(ChatMessage(
                    thread_id=thread.id,
                    author=msg["author"],
                    body=msg["body"],
                    mentions_agent=False,
                ))

    db.commit()

    # ── 5. Stub scenarios (raw SQLite) ─────────────────────────────────────
    # Must be seeded before we execute any agent runs, because the agents read
    # these scripts when making calls / portal checks.
    db_path = settings.database_url.removeprefix("sqlite:///")
    with sqlite3.connect(db_path) as conn:
        rows = []

        # Provider voice + portal scenarios
        for phone, prov in PROVIDERS.items():
            rows.append((
                f"provider:{phone}",
                json.dumps(prov["voice_scenario"]),
            ))
            rows.append((
                f"portal:{phone}",
                json.dumps(prov["portal_scenario"]),
            ))

        # Client check-in voice + email scenarios
        for client in CLIENT_CHECKINS:
            rows.append((
                f"provider:{client['phone']}",
                json.dumps(client["voice_scenario"]),
            ))
            rows.append((
                f"email:{client['email']}",
                json.dumps(client["email_scenario"]),
            ))

        # Staff/provider email scenarios
        for email_addr, scenario in EMAIL_SCENARIOS.items():
            rows.append((email_addr, json.dumps(scenario)))

        # Default fallback
        rows.append(("default", json.dumps([{"outcome": "no_answer", "spoken": ""}])))

        conn.executemany(
            "INSERT OR REPLACE INTO stub_scenarios (name, script, step) VALUES (?, ?, 0)",
            rows,
        )
        conn.commit()

    # ── 6. Open goals and runs through the platform runtime ────────────────
    import os
    from app.platform import runtime

    # Representative subset we actually execute now so the dashboard has real
    # agent-run and communication data on a fresh seed. The rest are opened as
    # pending runs that the scheduler can pick up later.
    EXECUTE_NOW: set[tuple[str, str, int]] = {
        ("Doe & Associates Injury Law", "PI-2026-0142", 0),  # Metro General records
        ("Doe & Associates Injury Law", "PI-2026-0142", 2),  # Maria Santos check-in
        ("Marchetti & Voss Injury Attorneys", "PI-2026-0201", 0),  # Elena Petrov check-in
    }

    opened_runs: list[tuple[tuple[str, str, int], AgentRun]] = []
    print("Opening goals and initial runs...")
    for info in task_infos:
        key = (info["firm_name"], info["case_number"], info["task_index"])
        goal, run = runtime.open_goal(
            firm_id=info["firm_id"],
            case_ref=str(info["case"].id),
            task_ref=str(info["task"].id),
            agent_config_id=info["agent_config_id"],
            brief=info["brief"],
            horizon=info["horizon"],
        )
        opened_runs.append((key, run))

        # Defer every run so the server scheduler does not race with us while
        # we seed. We will execute the representative set ourselves below.
        with SessionLocal() as db2:
            r = db2.get(AgentRun, run.id)
            r.next_run_at = datetime.now(timezone.utc) + timedelta(days=30)
            db2.commit()

    executed = 0
    if os.environ.get("MISTRAL_API_KEY"):
        to_execute = [(key, run) for key, run in opened_runs if key in EXECUTE_NOW]
        print(f"Executing {len(to_execute)} representative agent runs (MISTRAL_API_KEY is set)...")
        for j, (key, run) in enumerate(to_execute):
            firm_name, case_number, task_index = key
            print(f"  {firm_name} / {case_number} task {task_index} → run #{run.id}")
            runtime.execute_run(run.id)
            executed += 1
            # Brief pause between runs to stay within the free Mistral tier.
            if j < len(to_execute) - 1:
                time.sleep(15)
    else:
        print("MISTRAL_API_KEY not set; leaving all runs pending for the scheduler.")

    db.commit()

    # ── 7. Print summary ──────────────────────────────────────────────────
    print("=" * 70)
    print("SEED COMPLETE")
    print("=" * 70)
    print()
    print(f"  Platform firms: {db.query(PlatformFirm).count()}")
    print(f"  Agent configs:  {db.query(AgentConfig).count()}")
    print(f"  Triggers:       {db.query(Trigger).count()}")
    print(f"  CMS firms:      {db.query(Firm).count()}")
    print(f"  Cases:          {db.query(Case).count()}")
    print(f"  Tasks:          {db.query(Task).count()}")
    print(f"  Goals:          {db.query(Goal).count()}")
    print(f"  Agent runs:     {db.query(AgentRun).count()}")
    print(f"  Run events:     {db.query(RunEvent).count()}")
    print(f"  Communications: {db.query(Communication).count()}")
    print(f"  Escalations:    {db.query(Escalation).count()}")
    print(f"  Contacts:       {db.query(Contact).count()}")
    if os.environ.get("MISTRAL_API_KEY"):
        print(f"  Runs executed now: {executed}")
    print()

    print("─── Agent configs ───")
    for ac in db.query(AgentConfig).all():
        pf = db.get(PlatformFirm, ac.firm_id)
        print(f"  [{pf.name}] {ac.handle} → {ac.agent_name}  cadence={ac.cadence_days}d  skills={ac.skills()}")

    print()
    print("─── Cases by firm ───")
    for f in FIRMS:
        pf = platform_firms[f["name"]]
        cms_f = cms_firms[f["name"]]
        cases = db.query(Case).filter(Case.firm_id == cms_f.id).all()
        print(f"\n  {f['name']} (platform_firm #{pf.id}, cms_firm #{cms_f.id}):")
        for c in cases:
            tasks = db.query(Task).filter(Task.case_id == c.id).all()
            print(f"    {c.case_number} — {c.client_name}  [{len(tasks)} tasks]")

    print()
    print("─── Demo paths ───")
    print("  Task 1 (Metro General):    tag @records-agent → happy path → goal achieved")
    print("  Task 2 (County Records):   tag @records-agent → escalation (kind=task) → answer via dashboard/CMS/email → goal achieved")
    print("  Brightway Orthopedic task: tag @records-agent → fax-first → send_fax → portal release")
    print("  Task 3 (client check-in):  tag @checkin-agent → long-horizon goal → calls client → creates staff task")
    print()
    print("─── Skills ───")
    for f in FIRMS:
        print(f"  {f['name']}: {f['skills']}")


if __name__ == "__main__":
    main()
