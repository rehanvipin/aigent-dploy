# AGENTS.md

Operational notes for coding agents working in this repository. Project
context lives in BACKGROUND.md, ideal-state architecture in PLATFORM.md, and
current implementation details in GUIDE.md; this file is for how to build, run,
and not step on rakes.

## Build & run

- Everything runs through `uv` — never bare `python`/`pip`.
  - Serve: `uv run uvicorn app.main:app --host 127.0.0.1 --port 8000`
  - Seed: `uv run python scripts/seed.py` (idempotent; skips if firms exist)
  - Smoke-test Mistral: `uv run python scripts/mistral_smoke.py`
  - Fresh demo: kill server, `rm -f aigent-dploy.db`, start server, seed.
- One process serves everything: stub CMS (`/cms/...`, board at
  `/cms/board`), stub comms + portal (`/stubs/...`), platform API (`/api/...`),
  dashboard (`/`).
- Single SQLite file (`aigent-dploy.db`) shared by platform tables, stub CMS
  tables, and stub scenario/log tables (raw-SQLite tables, created by
  `app.main.STUB_DDL`, not SQLAlchemy).
- The LLM agent and voice path need `MISTRAL_API_KEY` in the environment.
  Without it, runs fail with a clear `MistralError` (no silent fallback).
- There are no tests; verification = `scripts/mistral_smoke.py` + the scripted
  end-to-end demo (seed → tag `@records-agent` on tasks 1 & 2 → run-now /
  answer escalation). Re-run it after touching runtime, tools, llm, or stub
  code. The LLM is non-deterministic: the agent may take a different (still
  valid) path through the tools; check the final status, not the exact event
  sequence.

## Quirks learned the hard way

- **Mistral SDK import surface.** This repo uses `mistralai` v2.x: the client
  is `from mistralai.client import Mistral` (NOT `from mistralai import
  Mistral`). Structured output is `client.chat.parse(response_format=PydanticModel, ...)`
  → `resp.choices[0].message.parsed`. TTS is `client.audio.speech.complete(...)`
  (returns base64 `audio_data`); STT is `client.audio.transcriptions.complete(model=..., file=File(file_name=..., content=bytes))`.
  Models present: `mistral-small-latest` (chat), `voxtral-mini-latest` (STT),
  `voxtral-mini-tts-latest` (TTS). Voice ids are slugs like `en_paul_neutral`
  / `gb_jane_neutral` — list with `client.audio.voices.list(type_="preset")`.
- **The Mistral SDK is blocking** — never call it on the event loop (same rule
  as the scheduler's HTTP calls). Scheduler already runs via
  `asyncio.to_thread`; webhook/API handlers are sync `def` (threadpool), so
  they're fine. Keep it that way if you add new call sites.
- **Never do blocking work on the event loop.** The scheduler makes blocking
  HTTP calls to endpoints in the same process; run inline it starves the
  event loop and those calls time out (`httpx.ReadTimeout` with no real
  cause). `scheduler_tick` must go through `asyncio.to_thread` (see
  `app/main.py`). Same rule applies anywhere in the lifespan/background code.
- **Starlette 1.6 removed the old `TemplateResponse(name, context)` form** —
  it now treats the context dict as the template name and dies with
  `TypeError: cannot use 'tuple' as a dict key`. Always use
  `templates.TemplateResponse(request, "name.html", {...})`.
- **`pkill -f uvicorn` in a compound command kills the compound command
  itself** (its own cmdline matches). Kill and start in separate tool calls.
- **Background servers**: `cmd &` alone gets killed when the shell call ends;
  use `(nohup ... &)` and give it a few seconds before health-checking.
- **Webhook + scheduler race**: the CMS chat webhook executes a new run
  synchronously, and the 2s scheduler tick can pick up the same run
  simultaneously (status was still `pending` in the scheduler's snapshot).
  `runtime.execute_run` guards with an in-process `_executing` set — keep
  that guard if you refactor.
- **Escalation answers from CMS chat**: the CMS stub forwards *every* staff
  message to the webhook, not just ones mentioning the agent; the platform
  decides (answer open escalation > run already active > start new run).
  If you change the trigger rule in `stubs/cms_api.py`, keep that ordering
  in `platform/api.py::cms_chat_webhook` in mind.
- **No `curl` in this environment** — use `uv run python -c "import httpx; ..."`
  or heredoc scripts for HTTP checks.

## Conventions

- Tools never get case/firm/task ids as arguments from the agent — the
  `Toolset` is constructed per-run and injects them (firm scoping, audit
  trail). Same rule for the LLM: tool schemas in `llm.py::TOOL_SPECS` expose
  only domain args; stub-internal keys (scenario, provider_key) are derived
  in the dispatcher. Don't expose them to the model.
- Tool results are deliberately free-form in their payloads (the CMS changes
  between firms); only the envelope (outcome / transcript / payload) is
  normalized. Agent code must read, not assume.
- An agent module contains only its declaration (name, tools, instructions,
  cadence) — decision logic lives in the LLM prompt, and the platform's
  `llm.py` drives it. A `step_fn` is still supported for deterministic
  agents, but scheduling, persistence, audit, and escalation plumbing belong
  to the platform — don't let them leak into `app/agents/`.
- Scenario scripts in `scripts/seed.py` are keyed by provider phone
  (`provider:+1-...`, `portal:+1-...`); each check/call advances the script
  one step and sticks on the last entry.
