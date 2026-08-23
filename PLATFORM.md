# Platform Design (ideal state)

This document describes the target design of the Aigent-Dploy agent platform —
what a production system should look like. For what this repository actually
implements, see the POC Architecture section in the README.

## Goal

Make it cheap and safe to put an AI agent on a piece of firm work: an engineer
should be able to ship an agent that pursues **goals** (records follow-up,
client check-in, adjuster follow-up, ...) by writing only that work's
instructions and tool allow-list. The platform owns everything else: triggers,
scheduling, state, the LLM loop, channels, speech, skills, memory, guardrails,
audit, and the path to a human.

## Core concepts

**Case is the unit of work; the goal is the unit of intent.** The firm's CMS is
the system of record for cases, work items, and contacts. The platform never
becomes the source of truth for business context; it reads context through the
firm's CMS connector and writes outcomes back, so a firm can adopt agents
without changing where its data lives. On top of case data, the platform tracks
**goals**: durable statements of intent ("get the records named in this task",
"keep this client updated until the case closes"). A goal can be short-lived or
run for months; it survives individual agent runs.

**An agent run = one contiguous execution toward one goal.** Agents own goals,
not conversations. The run is the unit of scheduling, audit, and escalation; a
goal is the durable thing that triggers bind to and that staff recognize. A
failed run does not kill the goal — staff can start a new run under it.

**Long-running = recurring invocations, not processes.** A run wakes, does one
unit of work, and either reschedules itself, escalates, or completes. Nothing
of consequence lives in memory; any wake can happen on any machine, weeks
apart. Follow-up cadence is data, not a sleeping thread.

**AI prepares, the lawyer/staff approves.** Agents do the legwork but do not
take judgment calls. Anything the agent is not confident about, and anything
client-facing or high-risk, is staged for a human — as a question to answer
*or* as work for a human to do and report back on.

**Multi-tenant by construction.** Shared infrastructure, firm-scoped data:
every goal, run, event, escalation, communication record, and skill binding
carries a firm id, and every tool call is executed in that firm's context (its
CMS connection, its credentials).

## Triggers

Triggers are **data, not code paths**. A trigger row binds an event type to
either an agent configuration (a *standing* trigger: "open a goal when ...") or
a specific goal (an *instance* trigger: "wake this goal when ..."). One routing
pipeline handles every inbound event, in this order: resolve an open
escalation → wake an active run → open a new goal.

Agents start from three kinds of events:

* **Staff message** — the primary path: staff tag the agent in the CMS task
  chat (or email the agent's address). The CMS fires a webhook; a standing
  trigger matched on the agent's handle opens a goal.
* **Schedule** — follow-up cadences (weekly, day 7/21/45, ...) wake existing
  runs. The schedule is materialized as `next_run_at` on the run, computed from
  the agent's cadence policy and what happened last time.
* **Inbound channel events** — a reply to an agent's email, a returned fax, a
  call back from a provider. Outbound comms mint a `conversation_key`; an
  instance trigger routes the reply to the open run it belongs to and wakes it
  with the new information.

## Connectors: the CMS abstraction

Firms do not all run the same CMS — some have Filevine or Litify, some manage
cases in spreadsheets and email. The platform therefore never talks to a
specific CMS directly; it talks through a **connector** behind a normalized
interface (`get_case`, `get_work_item`, `post_update`, `post_message`,
`create_work_item`, ...). The connector owns everything that must be
*enforced*: authentication, firm scoping, id mapping, retries. CMS identifiers
are **opaque refs** to the platform — `case_ref`, and an optional `task_ref`
for CMSs that have a task/work-item subconcept.

The connector envelope carries **capability flags** (`supports_chat`,
`supports_tasks`, ...) so the platform picks surfaces by capability — a
chat-less firm gets escalations by email and on the dashboard, not in a task
chat. Variance is data, never `if cms_type == ...` branches.

**Code enforces, skills inform.** The connector normalizes the envelope; a
co-located **skill** teaches the agent the CMS's *meaning*: what a "task" is in
this system, where staff actually watch for updates, which contact role is the
records clerk. One skill per connector, kept honest in the same change. For
CMSs with no API at all, the generic browser/email tools plus a skill *are* the
integration — same mechanism, no connector code.

## Agent authoring model

Agent configuration has two levels:

* **Agent type** (code) — a small module declaring **name and description**,
  **instructions** (a system prompt describing the goal type, the rules, and
  how to decide), the **tool universe** it may draw on, default **cadence
  policy**, and **done/stop conditions**. It contains no scheduling,
  persistence, channel, or audit code.
* **Per-firm agent config** (data) — a row binding an agent type to a firm:
  the staff-facing **handle** that triggers it, the **skill allow-list**,
  an optional **guardrail focus** (see below), and cadence overrides. Goals and
  triggers reference the config, so the same agent type behaves differently —
  and safely — per firm.

The platform drives the agent through an **LLM function-calling loop**:

1. build the context — the instructions, the freshly-fetched case/work-item
   through the connector, the run's scratchpad, the recent audit history, the
   attached **skill index** (names + one-line descriptions), and recent
   communications on the case — into a message list;
2. present the agent's allowed tools as function schemas (domain args only;
   firm/case refs and credentials are injected by the platform);
3. loop: the model returns tool calls, the platform executes them through the
   run-scoped toolset (mutating calls pass the **guardrail reviewer** first)
   and appends the results, until the model is ready to decide;
4. read the decision back as structured output: *wait* (with when), *done*
   (with the outcome), or *escalate* (with a question or a work request, plus
   full context).

A deterministic `step_fn` is still supported as an escape hatch, but the
default — and the point of the platform — is that decision logic lives in the
prompt, and the platform supplies the tools, the skills, the memory
(scratchpad + audit history + communications archive), the guardrails, and
escalation as a first-class result.

## The LLM brain

The platform talks to a single LLM provider (Mistral today, behind a thin
adapter so it can be swapped):

* **reasoning** — chat + function calling, with structured output for the
  per-step decision;
* **adversarial review** — the same chat capability, driven with an adversarial
  system prompt, for the guardrail reviewer;
* **speech** — text-to-speech (the agent's spoken side) and speech-to-text
  (transcribing the other party). A voice call is a real TTS→STT round trip,
  so the transcript the agent reasons over is genuinely spoken and heard,
  not a script. In production this plugs into a telephony provider; the
  structure (spoken line → outcome → transcript) stays the same.

All LLM calls are blocking and run off the event loop; failures surface as a
run failure with a clear cause rather than a silent fallback.

## Skill library

Skills are **knowledge, not configuration** — a folder of Markdown documents
the agent can pull in when the work demands it. Three flavors:

* **work-kind skills** — how to do one specific kind of work well: what to ask
  for when requesting emergency-trauma records, the medical and legal terms to
  recognize, what a complete records package contains;
* **system-operation skills** — how to operate a particular system: a
  provider's records portal (through the browser-automation tool), a specific
  CMS's quirks (co-located with its connector);
* **firm-context skills** — this firm's org chart (who to redirect a call to),
  preferences, and conventions. Firm skills live under the firm's namespace and
  are visible only to that firm's runs.

Skills are **not** an allow-list of capabilities — that is what the tool
allow-list is for. A skill tells the agent *how* to do work it is already
allowed to do.

**Progressive disclosure is a hard requirement.** The context message carries
only each attached skill's name and one-line description; the agent pulls the
body with a platform-level `load_skill(name)` tool when it judges the skill
relevant. Skill loads land in the audit trail. This keeps prompts small and
attention focused. (Production note: the platform may later *auto-suggest*
skills from context — provider named in the task → surface that portal's
skill — but loading stays explicit and audited.)

## Tools

Tools are the only way agents touch the world. Every call is authenticated in
the calling firm's context and written to the run's audit trail.

* **CMS** — through the firm's connector: read case/work-item/contact context;
  write back outcomes (notes, status changes, chat messages, documents
  received).
* **Voice** — place calls and hold conversations autonomously; a real TTS→STT
  round trip that returns a transcript plus a structured outcome.
* **Email / fax** — send and receive; outbound sends mint a `conversation_key`
  and an instance trigger so inbound replies are routed back to the owning run.
* **Browser automation** — for providers and on-prem systems without APIs
  (records portals, payer sites), reached through a controlled browser worker
  with per-firm credentials and network access. Portal-operation skills teach
  the agent each portal's flow.
* **Memory** — `search_conversations` / `read_conversation` over the
  communications archive, and `load_skill` over the skill library.
* **Escalate** — ask a human (see below).

Tool results are structured but their schemas vary — every firm's CMS and
every provider's portal is different. Agents must treat payloads as data to
read, not contracts to assume; the platform normalizes only the envelope
(outcome, transcript, raw payload). The LLM sees only domain arguments in the
tool schemas; firm/case refs, credentials, and stub-internal keys are derived
by the platform at dispatch time.

## Context and memory

The agent's context comes from three tiers, each with a distinct role:

* **The CMS is the system of record** for business context — case facts,
  contacts, work items, staff chat. Fetched live through the connector at run
  time; outcomes written back. Never duplicated as truth in the platform.
* **The scratchpad is working memory** — a JSON blob the agent maintains across
  invocations (a portal request id, staff answers received).
* **The communications archive is a derivative context store** — a private,
  firm-tagged copy of the calls, emails, and faxes the agent initiated or
  participated in. It exists because full transcripts and threads are too
  verbose for the CMS but valuable as agent context; outcomes still get written
  back to the CMS. Every row carries `firm_id` so entitlement checks can be
  layered on later. Search is by keyword in the POC; production adds vector
  retrieval.

## Guardrails

Before an agent performs an outward, world-mutating action, the platform can
run the proposal past an **adversarial reviewer**: a second LLM call with the
same context the agent saw (instructions, situation, recent events, loaded
skills) but an adversarial mindset — its job is to catch mistakes, not to
approve work.

* **Opt-in per agent config.** A non-empty `guardrail_focus` enables review;
  the string itself tells the reviewer the major kinds of issues to look out
  for ("never promise settlement figures; verify the provider name against the
  case contacts before sending"). Disabled configs pay no extra LLM calls.
* **Scope.** By default all outward/mutating calls are reviewed (voice, email,
  fax, portal submissions, CMS writes); reads are harmless and skip review.
  A per-config tool list can narrow or widen this.
* **Verdicts.** `allow` executes the call. `block` does not execute; the tool
  result is replaced with the block reason and fed back to the agent, which can
  revise its approach or escalate. The guardrail *constrains* the agent's
  judgment — it never replaces it.
* **Audit.** Every verdict (allow or block, with reasoning) is written to the
  run's audit trail; blocks are surfaced on the dashboard.

Known boundary: deterministic `step_fn` agents bypass the LLM tool loop, so
guardrails apply to LLM-driven agents only.

## Human-in-the-loop

Escalation is a first-class run outcome, not an error, and it comes in two
kinds:

* **question** — "the records bureau demands prepayment; do we pay?" —
  resolution is an answer;
* **task** — "a human must go do something" (sign and upload a form, call a
  personal contact) — resolution is a completion signal plus an optional note.

Either way:

1. The agent escalates with the question or work request and the full context
   of the work so far.
2. The run parks. The escalation appears where the firm's staff actually work —
   the CMS task chat if the connector supports chat, otherwise by email — and
   always on the platform dashboard (where admins see all firms).
3. A staff answer or completion note, given in **any** of those places — CMS
   chat, dashboard, or a plain email reply — is routed back through the trigger
   pipeline, recorded on the run, and wakes the agent, which folds it into its
   context and continues.

## State and audit

* The platform database stores **what agents did and what the platform decided
  about them**: goals, runs, the follow-up schedule, an append-only event log
  per run (every tool call, decision, guardrail verdict, skill load, trigger
  routing, escalation), escalation Q&A, and the communications archive.
  Business context stays in the CMS.
* Every action an agent takes is reconstructible from the audit log: what it
  saw, what skills it loaded, what it decided, which guardrail verdicts passed
  or blocked, who it contacted, what came back, and who approved anything that
  needed approval.
* The dashboard is a read view over this log: goals and active runs per firm,
  pending escalations, guardrail blocks, transcripts, and outcomes written back
  to the CMS.

## Infrastructure (ideal state)

* A **run queue** (durable) replaces the interval poller: wakes are messages
  due at a time, workers are stateless and horizontally scaled.
* **Channel providers**: a telephony provider for voice, transactional email,
  a fax API, and a pool of browser workers for portal automation, all behind
  the tool interface above.
* **Credential vault** per firm for CMS API keys and portal logins (the POC's
  `firms` table is the seed of this).
* **Vector retrieval** over the communications archive; **auto-suggested
  skills** from context.
* POC simplifications, for the record: SQLite instead of a real database, a
  single process instead of queue + workers, one stub CMS connector and stub
  channels instead of real ones, keyword search instead of vectors (the LLM
  brain, adversarial reviewer, and voice TTS/STT are real Mistral).
