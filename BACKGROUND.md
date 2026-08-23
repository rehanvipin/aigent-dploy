# Background: Personal Injury Firm Workflow

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

* **Medical Record follow up agent**: firm can tag the agent to follow up with providers or hospitals to get the client records. A provider is a better setup since they have an app which the agent can access (either via API or browser automation tools).
* **Client CheckIn agent**: the agent can call / text the client (can even text them via SMS) and get to know how they are doing, and give back updates to them regarding their case. The goal here is client communication to keep them informed, and maintain good relations with them.
* **Adjuster follow up agent**: once a demand is sent to the insurance company, the agent follows up with the adjuster on a set cadence (e.g., day 7 / 21 / 45 after the demand) till there is a response. It writes claim status updates back to the CMS and escalates to the lawyer when the adjuster disputes coverage or starts negotiating.
* **Lien follow up agent**: tracks the lien holders on a case (health insurers, medicare / medicaid, providers treating on a lien) and calls them periodically to verify and update lien balances, so the numbers are current when the demand is finalized and when the case settles.

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

## Open Questions

* Filevine uses a task board including a chat functionality. What about other CMS?
* Why does the first records request need to be sent by a human?
