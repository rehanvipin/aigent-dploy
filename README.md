# Long Running Agent Platform : Aigent-Dploy
PI firms help injured clients get insurance payments settled via legal routes.
Aigent-Dploy builds AI agents which can speed up their work and open new opportunities.

## Tasks Done By PI firms
There's a lot of manual work that needs to be done by staff / lawyers at a PI (personal injury) firm for a case.
In most cases, it is sent to an outsourcing agency.

* Find clients. Clients can either call them (which is handled by inbound call agents), or marketing agencies collect leads and share with them.
* Engage with the client to build a case, which may go to litigation in the end.
* The case is tracked in a case management system e.g., Filevine.
* Follow up with medical providers to get the client's medical records (which they need to pay for in some cases)
* Follow up with the insurance adjuster after a demand is sent, so the demand does not sit unanswered in the adjuster's queue.
* Track lien holders (health insurers, medicare, providers on liens) and verify their balances before the demand is finalized and again at settlement.
* Follow up with the client to see how they are doing, what treatments they are taking etc. till the case closes.

## Workflow Agents
AI agents can automate the work so that humans don't have to do it.
There are some agents already built / being built.
* Medical Record follow up agent : firm can tag the agent to follow up with providers or hospitals to get the client records. A provider is a better setup since they have an app which the agent can access (either via API or browser automation tools).
* Client CheckIn agent : the agent can call / text the client (can even text them via SMS) and get to know how they are doing, and give back updates to them regarding their case. The goal here is client communication to keep them informed, and maintain good relations with them.
* Adjuster follow up agent : once a demand is sent to the insurance company, the agent follows up with the adjuster on a set cadence (e.g., day 7 / 21 / 45 after the demand) till there is a response. It writes claim status updates back to the CMS and escalates to the lawyer when the adjuster disputes coverage or starts negotiating.
* Lien follow up agent : tracks the lien holders on a case (health insurers, medicare / medicaid, providers treating on a lien) and calls them periodically to verify and update lien balances, so the numbers are current when the demand is finalized and when the case settles.

### Medical Record Agent
When a case is created on filevine, the human can create multiple tasks under it.
One of the tasks can be to get medical records from a hospital where the client was treated.
The hospital itself maybe able to provide the records, or it may be through a provider.
The records can be received either via fax, email, or through a website which the provider has.
The client needs to sign a HIPAA form which says that the firm is authorized to get their medical records. This is then used by the outsourcing agency as well.

When the staff tags the agent in the chat (e.g., filevine chat), zapier sends a webhook API request to the server which starts the agent and tells it to look at the person's message and perform the relevant task.

If the person has asked the agent to follow up, the agent will use the tool for making voice calls or email tool and call the provider or hospital.

The agent can also make its own follow up schedule (based on a fixed schedule e.g., every week for critical cases or biweekly for normal cases).

The contact information of the provider / client and any background information about them is present in the CMS.

### Client Check In Agent
An agent to call the client on a periodic basis till the end of the case.
This is to ensure that the person is recovering well, and if needed, highlight to the law firm staff / lawyers if needed.
It should also give them updates from the last time that it spoke with them, and be able to answer questions about the case.

### Adjuster Follow Up Agent
A demand that is sent and not followed up ages in the adjuster's queue while the case stalls. Adjusters handle more files than they can respond to, so they respond to the firms that follow up consistently.
Once a demand goes out, the agent owns the follow up schedule (e.g., day 7 / 21 / 45 after the demand, then weekly till there is a response) and calls or emails the adjuster for a status update.
Every outcome is written back to the case in the CMS : no answer (the next attempt is queued automatically), a claim status update, or a response on the demand.
Anything that needs judgment (a coverage dispute, a reservation of rights letter, the start of negotiation) is escalated to the lawyer with the full context of the conversation so far.

### Lien Follow Up Agent
A PI case can have multiple lien holders : health insurers, medicare / medicaid, and providers who treated the client on a lien.
Their balances change over time, and the firm needs current numbers when the demand is finalized and again at settlement.
The agent tracks the lien holders on each case and calls them periodically to verify and update the balances, writing the updated numbers back to the CMS.
If a lien holder disputes the balance or the lien needs to be negotiated, the agent hands it off to the staff instead of guessing.

## Current Architecture of the system
The production system is proprietary, so all the information can't be included here.
* python service running on the cloud which is the backend server.
* dashboard web UI for admin users which shows the different tasks that the agent is tracking and performing actions on, the calls which it has made. Some of the law firm staff can also access calls & task trackers which are related to their firm.
* database which holds all information required for the dashboards or information about what the agents have done. it won't hold all the context since that will be on the law firm CMS.

## POC Architecture (this repository)
One local process runs three things, so the design is visible end to end:

* **Stub services** (`app/stubs/`) — stand-ins for everything external, with scripted responses so demos are repeatable:
  * a stub CMS (Filevine-like): firms, cases, tasks, contacts, and per-task chat, over a REST API plus a small task-board UI (`/cms/board`). Tagging the agent in a task chat POSTs a webhook to the platform (the Zapier in the real world).
  * stub comms channels: voice / email / fax. The voice stub supplies the *other party's* scripted reply (answered / records_ready / needs_payment / refused); the agent's side is a real Mistral TTS→STT round trip, so a call really is spoken and transcribed.
  * a stub provider records portal: the agent submits a records request (the browser-automation path) and polls its status.
* **Platform core** (`app/platform/`) — the part that makes new agents cheap:
  * *triggers*: CMS chat webhook, plus an interval scheduler for follow-ups.
  * *runtime*: a run is one agent assigned to one task within one case. Long-running behaviour is modelled as recurring scheduled invocations, never in-memory processes: a run wakes, does one unit of work, then reschedules itself (`next_run_at`), escalates, or completes.
  * *LLM brain* (`llm.py`): the Mistral function-calling loop and speech (TTS/STT). The loop passes the agent's tools as function schemas, executes the model's tool calls through the run-scoped `Toolset`, and reads the final decision back as structured output.
  * *tools* (`tools.py`): cms read/write, voice, email, fax, portal, escalate. Every call is recorded in the run's audit trail, and every run is firm-scoped (shared infra, firm-scoped data). The model sees only domain args; firm/case/task ids and stub-internal keys are injected by the platform.
  * *human-in-the-loop*: an escalation parks the run, posts the question into the CMS task chat, and shows up on the admin dashboard. A staff reply in either place resumes the run.
  * *state*: SQLite. The platform DB stores only what agents did (runs, events, escalations, schedule); business context stays in the CMS and outcomes are written back to it.
* **Agents** (`app/agents/`) — an agent is a single small module: name, description, allowed tools, cadence, and its `instructions` (system prompt). The platform drives it through the Mistral function-calling loop in `app/platform/llm.py`; an agent contains no scheduling, persistence, or audit code. The medical record follow-up agent is implemented as the reference (`medical_records.py`).

### Running the POC
```
export MISTRAL_API_KEY=...        # required: drives the LLM agent + voice TTS/STT
uv run uvicorn app.main:app       # serve on :8000
uv run python scripts/seed.py     # one firm, one PI case, two records tasks, scripted stubs
```
`uv run python scripts/mistral_smoke.py` sanity-checks chat, structured output, TTS, and STT.

Then open the dashboard (`/`) and the CMS task board (`/cms/board`). Tag `@records-agent` in a task chat to start a run. The seeded scenarios demo both paths:
* Metro General Hospital: portal request → phone follow-up → records released → task closed (happy path).
* County Records Bureau: demands prepayment → agent escalates → staff answers in the CMS chat or dashboard → agent resumes and finishes (human-in-the-loop path).

## Platform Design
The target design (goals, core concepts, triggers, tools, human-in-the-loop, state, infrastructure) lives in [PLATFORM.md](PLATFORM.md). It describes the ideal state, not what this repository implements.

The goal of this current repository is not to integrate with the existing system or try to improve it. It is to make a POC with stubbed services to show how the design would work. There should be a minimal working POC on the local system. The software doesn't need to be production grade but needs to show system design mostly.

## Open Questions
* Filevine uses a task board including a chat functionality. What about other CMS?
* Why does the first records request need to be sent by a human? 