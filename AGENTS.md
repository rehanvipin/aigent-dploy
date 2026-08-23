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
  The guardrail reviewer adds a second Mistral chat call per outward action,
  so a free-tier key can hit 429 rate limits: the loop retries chat with
  backoff and the guardrail fails open (audited as `guardrail UNAVAILABLE ...
  executed unreviewed`), so a 429 shows up as an audited event, not a dead run.
- Seeding creates a `platform_firms` row, two `agent_configs` (`@records-agent`
  with guardrail + 4 skills, `@checkin-agent`), and two standing triggers;
  skills live under `skills/`. Tagging a handle in CMS chat therefore opens a
  GOAL and starts its first run via the trigger pipeline — the webhook has no
  hardcoded agent anymore.
- There are no tests; verification = `scripts/mistral_smoke.py` + the scripted
  end-to-end demo: tag `@records-agent` on task 1 (happy path) and task 2
  (escalation — answer via dashboard, CMS chat, or simulated inbound email),
  and `@checkin-agent` on task 3 (case-scoped goal). Fresh-demo reset is
  unchanged (`rm -f aigent-dploy.db`, restart, seed). Re-run it after touching
  runtime, tools, llm, triggers, or stub code. The LLM is non-deterministic:
  the agent may take a different (still valid) path through the tools; check
  the final goal/run status, not the exact event sequence.

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
- **Trigger routing is centralized** in `app/platform/triggers.py::route_event`
  — one pipeline for every inbound event (CMS chat, inbound email), in order:
  open escalation on the work item > instance trigger (matched by
  `conversation_key`) > already-running run > standing trigger (matched by
  agent `@handle`) opens a new goal. If you change the trigger rule in
  `stubs/cms_api.py` (what it forwards), keep that ordering in mind.
- **ORM name collision**: `AgentRun.goal` is the brief *text* (a column); the
  relationship to the `Goal` row is `goal_row`. Don't rename one to the other.
- **Opaque refs, not ints**: `AgentRun.case_ref`/`task_ref` are opaque STRINGS
  (the CMS's ids, whatever shape); `task_ref` can be NULL for CMSs without a
  task subconcept or for case-scoped long-horizon goals. Don't cast them to int.
- **Guardrail reviewer blindness** (known limitation): the reviewer sees the
  snapshot context + the proposed action, but NOT other in-step tool results.
  It can contradict a status the agent just learned seconds earlier, or
  mis-classify an agent *reporting* a block as falsely claiming. Mitigations:
  the reviewer is told to take terminal tool statuses at face value and
  respect staff answers/chronology; agent instruction #9 tells it to escalate
  rather than resubmit when the same action is blocked twice for the same
  reason. A fuller fix (pass the in-step tool transcript to the reviewer) is a
  follow-up.
- **No `curl` in this environment** — use `uv run python -c "import httpx; ..."`
  or heredoc scripts for HTTP checks.

## Conventions

- Tools never get case/firm refs as arguments from the agent — the `Toolset`
  is constructed per-run (with the firm's CMS connector) and injects
  `case_ref`/`task_ref` (opaque strings) and firm scoping. Same rule for the
  LLM: tool schemas in `llm.py::TOOL_SPECS` expose only domain args;
  stub-internal keys (scenario, provider_key, conversation_key) are derived
  in the dispatcher. Don't expose them to the model.
- CMS access goes through the firm's **connector** (`app/platform/connectors.py`),
  not direct HTTP — the Toolset holds the connector resolved from the run's
  firm. Adding a CMS = a new connector class + a matching skill, not platform
  branches. Capability flags (`chat`, `tasks`) drive channel choice.
- Skills are **knowledge, not configuration**: the tool allow-list is still
  the capability boundary; a skill teaches the agent *how* to do allowed work.
  Skills attach via the per-firm `agent_configs` row; bodies load on demand
  through `load_skill` (progressive disclosure) and are audited.
- Outbound comms mint a `conversation_key` (stored in the `communications`
  archive) and an instance trigger, so inbound replies route back to the
  owning run. The archive is firm-tagged and agent-only (not a mirror of CMS
  chat); `search_conversations`/`read_conversation` are the memory tools.
- An agent module contains only its declaration (name, tools, instructions,
  cadence) — decision logic lives in the LLM prompt, and the platform's
  `llm.py` drives it. A `step_fn` is still supported for deterministic
  agents (but it bypasses the guardrail, which lives in the LLM tool loop),
  and scheduling, persistence, audit, and escalation plumbing belong to the
  platform — don't let them leak into `app/agents/`.
- Scenario scripts in `scripts/seed.py` are keyed by provider phone
  (`provider:+1-...`, `portal:+1-...`); each check/call advances the script
  one step and sticks on the last entry.
