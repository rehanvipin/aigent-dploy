# Platform Design (ideal state)

This document describes the target design of the Aigent-Dploy agent platform —
what a production system should look like. For what this repository actually
implements, see the POC Architecture section in the README.

## Goal

Make it cheap and safe to create a new workflow agent: an engineer should be
able to ship an agent that owns one routine (records follow-up, client
check-in, adjuster follow-up, ...) by writing only that routine's
instructions and tool allow-list. The platform owns everything else: triggers,
scheduling, state, the LLM loop, channels, speech, audit, and the path to a
human.

## Core concepts

**Case is the unit of work.** The firm's CMS is the system of record for cases,
tasks, and contacts. The platform never becomes the source of truth for
business context; it reads context from the CMS and writes outcomes back to
it, so a firm can adopt agents without changing where its data lives.

**An agent run = one agent assigned to one task within one case.** Agents own
routines, not conversations. The run is the unit of scheduling, audit, and
escalation.

**Long-running = recurring invocations, not processes.** A run wakes, does one
unit of work, and either reschedules itself, escalates, or completes. Nothing
of consequence lives in memory; any wake can happen on any machine, weeks
apart. Follow-up cadence is data, not a sleeping thread.

**AI prepares, the lawyer/staff approves.** Agents do the legwork but do not
take judgment calls. Anything the agent is not confident about, and anything
client-facing or high-risk, is staged for a human.

**Multi-tenant by construction.** Shared infrastructure, firm-scoped data:
every run, event, and escalation carries a firm id, and every tool call is
executed in that firm's context (its CMS connection, its credentials).

## Triggers

Agents start from three kinds of events:

* **Staff message** — the primary path: staff tag the agent in the CMS task
  chat (or assign a task to it). The CMS fires a webhook; the platform opens
  a run on that task.
* **Schedule** — follow-up cadences (weekly, day 7/21/45, ...) wake existing
  runs. The schedule is stored as data per run, computed from the agent's
  cadence policy and what happened last time.
* **Inbound channel events** — a reply to an agent's email, a returned fax, a
  call back from a provider. These are routed to the open run they belong to
  and wake it with the new information.

## Agent authoring model

An agent is a small module declaring:

* **name and description** — what routine it owns;
* **instructions** — a system prompt describing the goal, the rules of the
  routine, and how to decide;
* **allowed tools** — the platform enforces the allow-list;
* **cadence policy** — how often it follows up (fixed, or computed from case
  priority / last outcome);
* **done / stop conditions** — so a run always terminates.

The agent contains no scheduling, persistence, channel, or audit code. The
platform drives it through an **LLM function-calling loop**:

1. build the context — the instructions, the freshly-fetched case/task from
   the CMS, the run's scratchpad, and the recent audit history — into a
   message list;
2. present the agent's allowed tools as function schemas (domain args only;
   firm/case/task ids and credentials are injected by the platform);
3. loop: the model returns tool calls, the platform executes them through the
   run-scoped toolset and appends the results, until the model is ready to
   decide;
4. read the decision back as structured output: *wait* (with when), *done*
   (with the outcome), or *escalate* (with question + full context).

A deterministic `step_fn` is still supported as an escape hatch, but the
default — and the point of the platform — is that decision logic lives in the
prompt, and the platform supplies the tools, the memory (scratchpad + audit
history), the guardrails (allow-list, round cap), and escalation as a
first-class result.

## The LLM brain

The platform talks to a single LLM provider (Mistral today, behind a thin
adapter so it can be swapped):

* **reasoning** — chat + function calling, with structured output for the
  per-step decision;
* **speech** — text-to-speech (the agent's spoken side) and speech-to-text
  (transcribing the other party). A voice call is a real TTS→STT round trip,
  so the transcript the agent reasons over is genuinely spoken and heard,
  not a script. In production this plugs into a telephony provider; the
  structure (spoken line → outcome → transcript) stays the same.

All LLM calls are blocking and run off the event loop; failures surface as a
run failure with a clear cause rather than a silent fallback.

## Tools

Tools are the only way agents touch the world. Every call is authenticated in
the calling firm's context and written to the run's audit trail.

* **CMS** — read case/task/contact context; write back outcomes (task notes,
  status changes, chat messages, documents received).
* **Voice** — place calls and hold conversations autonomously; a real TTS→STT
  round trip that returns a transcript plus a structured outcome.
* **Email / fax** — send and receive; inbound replies are routed back to the
  owning run.
* **Browser automation** — for providers and on-prem systems without APIs
  (records portals, payer sites), reached through a controlled browser worker
  with per-firm credentials and network access.
* **Escalate** — ask a human (see below).

Tool results are structured but their schemas vary — every firm's CMS and
every provider's portal is different. Agents must treat payloads as data to
read, not contracts to assume; the platform normalizes only the envelope
(outcome, transcript, raw payload). The LLM sees only domain arguments in the
tool schemas; firm/case/task ids, credentials, and stub-internal keys are
derived by the platform at dispatch time.

## Human-in-the-loop

Escalation is a first-class run outcome, not an error:

1. The agent escalates with a question and the full context of the work so far.
2. The run parks. The question appears in the CMS task chat (where staff
   already work) and on the platform dashboard (where admins see all firms).
   If neither is answered in a configured window, it falls back to email.
3. A staff answer, wherever it is given, is recorded on the run and wakes the
   agent, which folds the answer into its context and continues.

## State and audit

* The platform database stores only **what agents did**: runs, the follow-up
  schedule, an append-only event log per run (every tool call, decision,
  escalation), and escalation Q&A. Business context stays in the CMS.
* Every action an agent takes is reconstructible from the audit log: what it
  saw, what it decided, who it contacted, what came back, and who approved
  anything that needed approval.
* The dashboard is a read view over this log: active runs per firm, pending
  escalations, transcripts, and outcomes written back to the CMS.

## Infrastructure (ideal state)

* A **run queue** (durable) replaces the interval poller: wakes are messages
  due at a time, workers are stateless and horizontally scaled.
* A **CMS connector layer**: one connector per CMS (Filevine first) behind a
  normalized interface, since each firm's CMS — and each CMS's chat — is
  different.
* **Channel providers**: a telephony provider for voice, transactional email,
  a fax API, and a pool of browser workers for portal automation, all behind
  the tool interface above.
* **Credential vault** per firm for CMS API keys and portal logins.
* POC simplifications, for the record: SQLite instead of a real database, a
  single process instead of queue + workers, and stub CMS/channels instead of
  real connectors (the LLM brain and voice TTS/STT are real Mistral).
