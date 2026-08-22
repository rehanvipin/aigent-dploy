"""Mistral integration: chat, structured output, speech (TTS/STT), and the
tool-calling loop that drives LLM agents.

This module is the bridge between the platform's `Toolset` (which is already
firm/case/task-scoped and audit-logged) and the Mistral function-calling API.
The loop lives here, not in `app/agents/`, so agents stay declarative.

All calls are blocking; callers must keep them off the event loop (the
scheduler already runs in `asyncio.to_thread`).
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Any, Callable

from pydantic import BaseModel, Field

from app.config import settings
from app.platform.agent_base import RunContext, StepResult

log = logging.getLogger("llm")


class MistralError(RuntimeError):
    """Raised when Mistral is unavailable or misconfigured; surfaces on the run."""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

_client = None


def _mistral():
    global _client
    if _client is None:
        if not settings.mistral_api_key:
            raise MistralError(
                "MISTRAL_API_KEY is not set; the LLM agent cannot run. "
                "Set it in the environment and restart the server."
            )
        from mistralai.client import Mistral
        _client = Mistral(api_key=settings.mistral_api_key)
    return _client


# ---------------------------------------------------------------------------
# Chat + structured output
# ---------------------------------------------------------------------------

def chat(messages: list[dict], tools: list[dict] | None = None,
         temperature: float | None = None) -> Any:
    """One chat completion. Returns the SDK message (has .content and .tool_calls)."""
    client = _mistral()
    kwargs: dict[str, Any] = {
        "model": settings.mistral_chat_model,
        "messages": messages,
        "temperature": settings.mistral_temperature if temperature is None else temperature,
    }
    if tools:
        kwargs["tools"] = tools
        kwargs["tool_choice"] = "auto"
    try:
        resp = client.chat.complete(**kwargs)
    except Exception as exc:  # noqa: BLE001 - surface any SDK/HTTP error clearly
        raise MistralError(f"Mistral chat failed: {exc}") from exc
    if not resp.choices:
        raise MistralError("Mistral returned no choices")
    return resp.choices[0].message


class StepDecision(BaseModel):
    """Structured decision an LLM agent must produce at the end of a step."""
    action: str = Field(description="one of: wait | done | escalate | fail")
    note: str = Field(description="short human-readable summary for the audit trail / CMS")
    wait_days: float = 1.0
    escalation_question: str = ""
    escalation_context: str = ""
    scratchpad: dict[str, Any] = Field(default_factory=dict,
                                       description="working memory to persist across invocations")


def decide(messages: list[dict]) -> StepDecision:
    """Ask the model for its final structured decision for this step."""
    client = _mistral()
    try:
        parsed = client.chat.parse(
            model=settings.mistral_chat_model,
            messages=messages,
            response_format=StepDecision,
            temperature=settings.mistral_temperature,
        )
    except Exception as exc:  # noqa: BLE001
        raise MistralError(f"Mistral structured decision failed: {exc}") from exc
    try:
        return parsed.choices[0].message.parsed
    except Exception as exc:  # noqa: BLE001
        raise MistralError(f"could not parse Mistral decision: {exc}") from exc


# ---------------------------------------------------------------------------
# Speech (TTS / STT)
# ---------------------------------------------------------------------------

def synthesize(text: str, voice: str | None = None) -> bytes:
    """Text-to-speech; returns audio bytes."""
    client = _mistral()
    try:
        resp = client.audio.speech.complete(
            input=text,
            model=settings.mistral_tts_model,
            voice_id=voice or settings.mistral_tts_voice,
            response_format=settings.mistral_tts_format,  # type: ignore[arg-type]
        )
    except Exception as exc:  # noqa: BLE001
        raise MistralError(f"Mistral TTS failed: {exc}") from exc
    return base64.b64decode(resp.audio_data)


def transcribe(audio: bytes) -> str:
    """Speech-to-text; returns the transcript text."""
    client = _mistral()
    from mistralai.client.models import File
    try:
        resp = client.audio.transcriptions.complete(
            model=settings.mistral_stt_model,
            file=File(file_name="audio.mp3", content=audio),
        )
    except Exception as exc:  # noqa: BLE001
        raise MistralError(f"Mistral STT failed: {exc}") from exc
    return resp.text or ""


# ---------------------------------------------------------------------------
# Tool-calling loop
# ---------------------------------------------------------------------------

# (name, schema, dispatcher). Dispatcher receives (toolset, ctx, args) and
# returns a dict result. The model sees only domain args; firm/case/task ids
# and stub-internal keys are injected by the platform here.
ToolSpec = tuple[str, dict, Callable[[Any, RunContext, dict], dict]]


def _spec(name: str, description: str, properties: dict, required: list[str],
          dispatch: Callable[[Any, RunContext, dict], dict]) -> ToolSpec:
    return (name, {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }, dispatch)


def _resolve_provider_key(ctx: RunContext, provider_name: str) -> str:
    contacts = ctx.case.get("contacts", [])
    for c in contacts:
        if c.get("name", "").lower() == provider_name.lower():
            return c.get("phone") or c.get("name")
    return provider_name


TOOL_SPECS: dict[str, ToolSpec] = {}


def _register():
    TOOL_SPECS.update({
        "cms_post_message": _spec(
            "cms_post_message",
            "Post a message into this task's CMS chat thread.",
            {"body": {"type": "string", "description": "message text"}},
            ["body"],
            lambda t, ctx, a: t.cms_post_message(ctx.run.task_id, a["body"]),
        ),
        "cms_write_task": _spec(
            "cms_write_task",
            "Write an outcome note (and optionally a status) back to the CMS task.",
            {
                "note": {"type": "string", "description": "what happened / what to record"},
                "status": {"type": "string", "description": "optional task status"},
            },
            ["note"],
            lambda t, ctx, a: t.cms_write_task(ctx.run.task_id,
                                              status=a.get("status"), note=a["note"]),
        ),
        "voice_call": _spec(
            "voice_call",
            "Place a phone call and hold a conversation. Returns an outcome and a transcript.",
            {
                "to": {"type": "string", "description": "phone number to dial"},
                "purpose": {"type": "string", "description": "what you are calling about"},
            },
            ["to", "purpose"],
            lambda t, ctx, a: t.voice_call(to=a["to"], purpose=a["purpose"]),
        ),
        "send_email": _spec(
            "send_email",
            "Send an email.",
            {
                "to": {"type": "string", "description": "recipient email"},
                "subject": {"type": "string"},
                "body": {"type": "string"},
            },
            ["to", "subject", "body"],
            lambda t, ctx, a: t.send_email(a["to"], a["subject"], a["body"]),
        ),
        "send_fax": _spec(
            "send_fax",
            "Send a fax.",
            {
                "to": {"type": "string", "description": "fax number"},
                "document": {"type": "string", "description": "document contents"},
            },
            ["to", "document"],
            lambda t, ctx, a: t.send_fax(a["to"], a["document"]),
        ),
        "portal_request_records": _spec(
            "portal_request_records",
            "Submit a medical-records request on the provider's portal.",
            {
                "provider_name": {"type": "string", "description": "provider/hospital name"},
                "client_name": {"type": "string"},
                "case_number": {"type": "string"},
            },
            ["provider_name", "client_name", "case_number"],
            lambda t, ctx, a: t.portal_request_records(
                provider_key=_resolve_provider_key(ctx, a["provider_name"]),
                client_name=a["client_name"], case_number=a["case_number"],
            ),
        ),
        "portal_check_request": _spec(
            "portal_check_request",
            "Check the status of a submitted portal records request.",
            {"request_id": {"type": "integer"}},
            ["request_id"],
            lambda t, ctx, a: t.portal_check_request(a["request_id"]),
        ),
    })


_register()


def _tool_schemas(agent_tools: list[str]) -> list[dict]:
    return [TOOL_SPECS[n][1] for n in agent_tools if n in TOOL_SPECS]


def _context_message(ctx: RunContext) -> dict:
    """Build the single user message describing the current situation."""
    recent = []
    for e in (getattr(ctx.run, "events", None) or [])[-settings.mistral_history_events:]:
        recent.append({"kind": e.kind, "summary": e.summary, "at": e.created_at.isoformat()})
    payload = {
        "goal": ctx.run.goal,
        "attempt": ctx.run.attempt,
        "case": {
            "case_number": ctx.case.get("case_number"),
            "client_name": ctx.case.get("client_name"),
            "summary": ctx.case.get("summary"),
            "contacts": ctx.case.get("contacts", []),
        },
        "task": {"title": ctx.task.get("title"), "status": ctx.task.get("status"),
                 "notes": ctx.task.get("notes")},
        "scratchpad": ctx.scratchpad,
        "recent_events": recent,
    }
    return {
        "role": "user",
        "content": "Here is the current situation. Do one unit of work now, calling tools as "
                   "needed, then return your decision.\n\n" + json.dumps(payload, default=str),
    }


def run_llm_step(agent, ctx: RunContext) -> StepResult:
    """Drive one agent step through Mistral function calling, and return the
    platform's StepResult. Raises MistralError on any upstream failure."""
    messages: list[dict] = [
        {"role": "system", "content": agent.instructions},
        _context_message(ctx),
    ]
    tools = _tool_schemas(agent.tools)

    client = _mistral()
    for _ in range(settings.mistral_max_tool_rounds):
        kwargs: dict[str, Any] = {
            "model": settings.mistral_chat_model,
            "messages": messages,
            "temperature": settings.mistral_temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        try:
            resp = client.chat.complete(**kwargs)
        except Exception as exc:  # noqa: BLE001
            raise MistralError(f"Mistral chat failed: {exc}") from exc
        msg = resp.choices[0].message

        if not msg.tool_calls:
            # no more tool calls -> the model is ready to decide
            break

        # append the assistant's tool-call message
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {"name": tc.function.name,
                                 "arguments": tc.function.arguments},
                }
                for tc in msg.tool_calls
            ],
        })

        # execute each tool via the scoped Toolset and append the results
        for tc in msg.tool_calls:
            name = tc.function.name
            if name not in TOOL_SPECS:
                result = {"error": f"unknown tool {name!r}"}
            else:
                try:
                    args = json.loads(tc.function.arguments) if tc.function.arguments else {}
                except ValueError:
                    args = {}
                dispatch = TOOL_SPECS[name][2]
                result = dispatch(ctx.tools, ctx, args)
            messages.append({
                "role": "tool",
                "name": name,
                "content": json.dumps(result, default=str),
                "tool_call_id": tc.id,
            })
    else:
        # loop exhausted without a clean stop; force a decision anyway
        log.warning("tool-call round cap reached for run %s", ctx.run.id)

    messages.append({
        "role": "user",
        "content": "Return your final structured decision now "
                   "(action: wait | done | escalate | fail).",
    })
    decision = decide(messages)

    result = StepResult(
        action=decision.action,
        note=decision.note,
        wait_days=decision.wait_days,
        escalation_question=decision.escalation_question,
        escalation_context=decision.escalation_context,
    )
    # persist the agent's declared working memory
    if decision.scratchpad:
        merged = ctx.scratchpad
        merged.update(decision.scratchpad)
        ctx.save_scratchpad(merged)
    return result
