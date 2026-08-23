# Firm CMS basics — how work is organized here (stub CMS)

This firm's case management system organizes work like this:

- A **case** is the top-level matter (one client, one claim). It has a case
  number, a client, a free-text summary, **contacts**, and **tasks**.
- **Contacts** hang off the case and carry a `role`: `client`, `provider`,
  `hospital`, `adjuster`. Always use the contact whose role matches the job —
  for medical records you want the `hospital` or `provider` contact, never the
  client. The contact's `details` field holds free-form notes from staff and
  often tells you *how* that party likes to be reached.
- A **task** is one unit of work under a case (e.g. "get records from X").
  Each task has a **chat thread** that staff actually read — short, factual
  updates go there. Write outcome notes back to the task so the system of
  record stays current; close the task (`status=done`) only when its outcome
  is truly complete.
- Staff watch the task chat. If you need a human, post there — and remember
  they can also answer on the platform dashboard or by replying by email.

Etiquette: keep chat messages short and factual; never post medical details
beyond what the task already contains; never promise dates or amounts.
