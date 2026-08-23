# Metro General Hospital records portal — how to operate it

Metro General releases medical records only through its provider portal; fax
and email requests are ignored. The portal flow for a records request:

1. Submit the request once (provider name, client name, case number). You get
   back a request id — **store it in your scratchpad immediately**; the portal
   has no way to look up requests by patient afterwards.
2. Poll the request status with the request id. Statuses:
   - `submitted` / `processing` — queued; nothing to do but wait and re-check.
   - `awaiting_payment` — the portal is holding the request until an invoice
     is paid. Agents never pay invoices; escalate to staff.
   - `released` — the records are out. The task is done; tell the staff.
3. If a status stays `processing` for more than a couple of checks, call the
   records line (+1-555-0900) — a human can nudge the queue.

Practical notes:

- The records desk picks up during business hours; evenings go to voicemail.
- When you call, have the case number and the client's full name ready — they
  will not discuss a request without both.
- If anyone on the records line is unsure which department handled the visit,
  ask for the **Health Information Management** desk — they own all releases.
