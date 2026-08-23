"""Adversarial guardrail review for outward agent actions.

Optional per agent config: a non-empty ``guardrail_focus`` enables it, and the
string itself tells the reviewer the major kinds of issues to look out for.
Before a mutating tool call executes, the reviewer — a second LLM call with the
same context the agent saw but an adversarial mindset — returns a structured
verdict. ``allow`` executes; ``block`` does not execute and the reason is fed
back to the agent, which can revise its approach or escalate. The guardrail
constrains the agent's judgment; it never replaces it.

Every verdict lands in the run's audit trail (``kind="guardrail"``).

Boundary: the review lives inside the LLM function-calling loop, so
deterministic ``step_fn`` agents are not covered (documented in PLATFORM.md).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from app.config import settings

log = logging.getLogger("guardrail")

# Outward/world-mutating tools are reviewed by default; reads skip review.
DEFAULT_REVIEWED_TOOLS = {
    "voice_call", "send_email", "send_fax",
    "portal_request_records", "cms_write_task", "cms_post_message", "cms_create_task",
}


class GuardrailVerdict(BaseModel):
    verdict: str = Field(description="one of: allow | block")
    reasoning: str = Field(description="what you checked and why you reached this verdict")


ADVERSARIAL_PROMPT = """You are an adversarial safety reviewer for an AI agent working at a
personal-injury law firm. The agent is about to perform the OUTWARD ACTION
shown below — a call, email, fax, portal submission, or a write to the case
management system. Once executed it cannot be unsent.

Your job is to CATCH REAL PROBLEMS, not to approve work — but also not to
invent them. Judge the action against the tool's ACTUAL argument list below
and the case context; never demand parameters, fields, or identifiers that the
tool does not accept, and never assume requirements (forms, identifiers,
pre-conditions) that are not evidenced in the agent's instructions, the loaded
skills, or the case context.

Major issues this deployment must watch for:
{focus}

Also always check:
- wrong recipient (person, number, address, or provider not matching the case
  contacts),
- statements the firm must not make (settlement promises, legal advice,
  admissions, invented facts about the case),
- PII going to a party who should not have it,
- irreversible or high-risk actions taken without staff approval.

Take the tools' own results at face value: if a portal/status tool returned
'released' (or an equivalent terminal status), the records ARE released per
that system — do not demand further proof or block a factual report of it.
Only block a status report that affirmatively contradicts the most recent
tool result in the context.

Escalations and status notes to the firm's own staff are LOW RISK: blocking
one leaves the case stalled, which is usually worse than an imperfect message.
Block an internal note/escalation only if it is affirmatively harmful (wrong
case, leaks PII externally, states something false as fact).

Respect chronology and authority: entries in the context carry timestamps, and
the current time is given as "now". A staff instruction or answer (in
scratchpad.staff_answers, an inbound message, or a recent event) is the firm's
own word and OUTRANKS your earlier suspicion — if staff confirmed the
recipient, provider, or course of action, do not block on the doubt that was
already answered. A blocked tool result is also just a past reviewer's
opinion, not a standing order: re-judge the action against the context as it
stands now.

Be concrete: block only for a real, articulable problem visible in the evidence
above — a healthy dose of suspicion, not paralysis. If the action is ordinary,
correctly addressed, and consistent with the case context, allow it."""


def reviewed_tools(focus: str, tools_override: list[str] | None) -> set[str]:
    """Which tools get reviewed for this config. Default: all mutating tools."""
    if tools_override:
        return set(tools_override)
    return set(DEFAULT_REVIEWED_TOOLS)


def review_action(
    focus: str,
    tool_name: str,
    args: dict,
    agent_instructions: str,
    context_payload: dict,
    tool_schema: dict | None = None,
) -> GuardrailVerdict:
    """Adversarially review a proposed tool call. Raises MistralError on
    upstream failure (the run fails loudly rather than executing unreviewed)."""
    from app.platform import llm  # late import: llm imports nothing from here

    schema_text = json.dumps(
        (tool_schema or {}).get("function", {}).get("parameters", {})
    ) or "(no schema available)"
    client = llm._mistral()
    messages = [
        {"role": "system", "content": ADVERSARIAL_PROMPT.format(focus=focus or "(no focus given; apply the general checks)")},
        {"role": "user", "content": (
            "The agent's instructions and its current situation follow, then the "
            "action it wants to take. Review the action adversarially and return "
            "your verdict.\n\n"
            "=== AGENT INSTRUCTIONS ===\n" + agent_instructions + "\n\n"
            "=== CURRENT SITUATION (what the agent sees) ===\n"
            + json.dumps(context_payload, default=str) + "\n\n"
            "=== PROPOSED ACTION ===\n"
            f"tool: {tool_name}\nargs: {json.dumps(args, default=str)}\n\n"
            "=== TOOL ARGUMENT SCHEMA (the only fields this tool accepts) ===\n"
            + schema_text
        )},
    ]
    try:
        parsed = client.chat.parse(
            model=settings.mistral_chat_model,
            messages=messages,
            response_format=GuardrailVerdict,
            temperature=settings.mistral_temperature,
        )
        return parsed.choices[0].message.parsed
    except Exception as exc:  # noqa: BLE001
        raise llm.MistralError(f"guardrail review failed: {exc}") from exc


def audit_verdict(run, tool_name: str, verdict: GuardrailVerdict) -> None:
    from app.platform.db import SessionLocal
    from app.platform.models import RunEvent

    with SessionLocal() as db:
        db.add(RunEvent(
            run_id=run.id, firm_id=run.firm_id, kind="guardrail",
            summary=f"guardrail {verdict.verdict.upper()} on {tool_name}: {verdict.reasoning[:200]}",
            detail=json.dumps({"tool": tool_name, "verdict": verdict.verdict,
                               "reasoning": verdict.reasoning}),
        ))
        db.commit()
