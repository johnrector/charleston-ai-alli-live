# Charleston AI — Alli Calls

## Upgrade policy — owner direction, October 2, 2026

John wants this plugin to stay on the latest and best supported GPT-Live stack.
Treat continued modernization as the default when maintaining this project;
do not freeze it permanently on today's model IDs or Twilio SDK version.

- Preserve GPT-Live's full-duplex voice architecture and its own delegated
  reasoning/agents for in-call calendar and email work. Do not switch to the
  GPT-Realtime family merely because its version number is higher.
- Check current official OpenAI and Twilio documentation and account access
  before selecting an upgrade. Evaluate the voice model, delegated reasoning
  model, and Twilio Agent Connect integration separately. Prefer the newest
  supported combination that meets this application's requirements.
- Validate a candidate before promoting it to the demo: startup, prompt and
  tool compatibility, interruption behavior, latency, per-call isolation,
  calendar/email delegation, truthful outcomes, and duplicate prevention.
  Use offline/read-only checks first; real calls and external writes still
  need authorization for their particular recipient and purpose.
- Pin the tested production combination and retain a rollback version. If a
  newer option is incompatible or regresses behavior, keep the working version
  temporarily and document the exact blocker and candidate to revisit.
- Keep audience claims precise and verified: distinguish the GPT-Live voice
  model from the delegated reasoning model and SDK. Do not claim every part is
  the newest merely because the voice layer is current.

This is maintenance guidance, not a scheduled monitor, blanket permission for
external actions, or a requirement to change production automatically on release.

## Cloud plugin description

Make mission-driven outbound phone calls with Alli on John's behalf: confirm or reschedule appointments, ask questions, coordinate work, and follow up. No business card or fixed script required. GPT-Live handles the conversation with its own delegated reasoning and calendar/email tools. Calls run while the dot continues other tasks; retrieve saved results afterward. Use other connected tools for Shopify, Maps, invoices, and standalone email.

## Dot workflow

The dot coordinates the whole request; this plugin supplies the phone capability.
Use the connected cloud tools directly, without a Mac/browser relay.
During each call, GPT-Live retains its own delegated reasoning and calendar/email
tools. The dot need not handle those in-call actions or wait for the call to end.
This update changes no voice model, reasoning model, TAC version, audio settings,
delegation mechanism, credentials, or tool implementations.

1. Split the request into concrete outcomes. Start independent authorized work
   without waiting for calls to finish. Resolve necessary dependencies first.
2. Use current connected sources for recipients, phone numbers, appointment
   times, order addresses, and billing details. Never invent a missing contact,
   rate, location, or result. Ask only for essential missing information and
   continue unrelated work.
3. For each authorized phone conversation, prefer `call_outbound`. Supply its
   verified recipient, a clear mission, only relevant shareable context, the
   capabilities needed for the task, and a stable unique request ID. No card,
   image, or preset is needed. Optional presets guide the opening only.
4. For confirmation with possible rescheduling, provide the appointment time
   and timezone, and grant check_calendar/manage_calendar. Grant create_meeting
   only if new bookings are authorized, and send_email only for authorized
   related email. Explicit [] means conversation only. Existing owner-enabled
   defaults remain available when capabilities are omitted.
5. Preserve returned request IDs. Accepted/queued means dialing started, not
   that someone answered. Read progress and outcomes with `get_call_result`;
   never redial an uncertain attempt using a new ID. A busy-recipient response
   points to the existing intent and is not a reason to bypass deduplication.
6. Use Shopify/Maps, billing, and email tools directly for non-phone tasks.
   An invoice email does not require a call. Route links are not proof a route
   was saved to Maps; an email request is not proof of delivery; a call report
   is not independent verification of a calendar change.
7. Briefly acknowledge the overall work and report each outcome separately
   once verified. Do not force every response to be only "Calling."

These instructions grant no new permissions and start no work by themselves.
Illustrative demos are not live authorizations. Automatic future calls remain
off unless separately configured and authorized.

## Example task mapping (do not execute)

| Spoken work | Dot coordinates | Alli phone mission |
| --- | --- | --- |
| Put unfulfilled Shopify orders on a Maps route | Read orders, resolve addresses and route start/end, create the supported route artifact | None unless a call is explicitly requested |
| Confirm afternoon appointments; reschedule if needed | Read the actual calendar, resolve contacts, start each authorized call, inspect results | Confirm the identified meeting and agree a new time if needed; update the existing event |
| Email Blake an invoice for three hours onsite | Resolve Blake, the agreed rate and billing details; create invoice and send through the appropriate tools | None |

## Refresh after deployment

Update the private cloud plugin's description with the text above, refresh its
MCP tools, and verify call_outbound/get_call_result in the dot's cloud context.
Use a saved call result for the connectivity test. Real call behavior requires
a separately authorized recipient and mission; local tests do not establish it.

## Two-way number — October 2, 2026

The approved number is +1 854 444 7852. Inbound voice uses `/inbound/voice`
with the same pinned GPT-Live voice and Responses delegation architecture.
SMS uses `/inbound/sms` and the same reasoning model and mission-scoped tools.
Recent outbound missions (30 days) and inbound history (7 days) are joined by
normalized international phone number. Greet recognized people by first name;
confirm their identity and agreement to continue the prior mission before
disclosing details or using calendar/email tools. Unknown or ambiguous callers can leave a message only.
Phone/name matching is conversational confirmation, not strong authentication;
never handle sensitive account changes through it.

`list_communication_updates(after_id, limit)` is a read-only, durable feed for
the cloud dot. Persist `next_cursor` only after processing; fetch additional
pages until empty. Group updates by interaction SID to avoid duplicate alerts.
Notify John about meaningful completed changes, questions, delivery failures or
attention needed. An accepted/submitted SMS is not proof of delivery. Reports
are model-reported; action IDs and successful tool responses provide stronger
completion evidence. Never automatically redial/resend uncertain interactions.

Voice webhook failure uses the existing Netlify callback route, which forwards
to +1 843 327 6008. A disconnected media stream also returns to that fallback.
A 15-second no-audio watchdog closes a silent stream to trigger fallback.
The service is one always-on Render process; transport sessions are in-memory,
while missions, SMS work, transcripts and owner updates are in Postgres.
Uncertain work is surfaced for review after a worker interruption and is never
blindly retried. The cloud dot's monitoring schedule is configured separately.

Names explicitly introduced by inbound callers or texters are stored in the cloud
for greetings across both channels. SMS includes the recent exchange even for
new contacts. `ALLI_OWNER_PHONE` identifies John for a distinct owner greeting;
this does not grant new calendar/email permissions. Never ask John whether he
is returning his own call. Ambiguous shared numbers receive a neutral greeting.

## Completion reporting — owner-approved October 2, 2026

For John’s authorized appointment-confirmation work, complete the work in the
connected apps, not just in chat. The cloud dot owns this post-call workflow;
GPT-Live continues to own its in-call delegated reasoning and tools.

1. At dispatch, persist a task identifier, call request IDs, matching calendar
   event IDs, recipient identities, route results and per-item reporting state.
   Re-read current events and orders at execution time. Preparation is not
   permission to start the demo calls.
2. Continue independent tasks while calls run. Poll get_call_result in the active
   task. Treat provider/disposition and transcripts as evidence, not the stale
   queued label alone. Never retry uncertain calls under new IDs.
3. For each ended appointment call, read the exact current event and append or
   replace one delimited Allie follow-up block keyed by request_id. Preserve the
   original description, times, guests, location and recurrence. Include Eastern
   timestamp, confirmed/declined/unresolved attendance, evidenced laptop reminder,
   and relevant logistical questions/change requests. Voicemail is not attendance
   confirmation. If only a transcript exists, summarize conservatively. Never
   claim a requested calendar change actually happened without action evidence.
4. Event descriptions may be visible to attendees. Include only this event’s
   scheduling facts; put private comments in John’s email. Refresh immediately
   before patching and read back afterward. If a concurrent edit is detected or
   write outcome is uncertain, re-read and reconcile without duplicating blocks.
5. When all items are terminal or have a genuine blocker, send John one completion
   email via the existing connected Gmail account. Use subject
   “Allie — Work completed — <task identifier>”. Sign “Allie, John Rector’s AI
   assistant.” Include actual call outcomes, verified calendar-note updates, Maps
   driving links, missing-address/future-order exclusions, and unresolved items.
   Clearly label incomplete work. Links are not saved Maps routes.
6. Persist the returned Gmail message ID and verify it by reading it back. For
   an uncertain send, search Sent for the exact unique task identifier before
   considering a retry. No blind redial, duplicate note, or duplicate email.

Use John’s connected sender/account; do not invent an Allie sender address,
create a mailbox, or change sender authentication. A separate address is optional
branding, not a technical dependency. The completion report goes to John; other
recipients require their own task authorization. This workflow grants no new
calling, rescheduling, cancellation, or attendee-email authority.

## Owner SMS capabilities — October 2, 2026

Twilio-signed inbound SMS from the configured ALLI_OWNER_PHONE can now read the
primary calendar and send owner-requested emails using the existing Responses
reasoning model and Google email implementation. Caller-name claims and voice
caller ID do not enable these owner SMS tools. Public callback permissions and
GPT-Live voice/delegation are unchanged.

Calendar lookup defaults to today through 14 days ahead and supports bounded
windows up to 31 days. Match appointment titles, participants and organizer names.
Email recipients resolve from nearby calendar participants or previous authorized
call contacts; this is not a complete address book. An explicit address from the
owner or one unambiguous lookup is required. Unknown/ambiguous names require a
question. A complete explicit send instruction is sufficient; do not ask for
redundant approval. Saved action receipts and existing durable send deduplication
prevent blind retries after uncertainty. The service cannot read the inbox.

SMS remains separate from the cloud dot: no Shopify/Maps tools, outbound dialing,
or arbitrary dot task handoff have been added. Owner phone matching authorizes
these bounded SMS conveniences, not account/security changes. Reassigning the
owner number requires updating ALLI_OWNER_PHONE.

## Outbound voicemail ringing window — October 3, 2026

Allow up to 60 seconds of ringing before declaring no answer (previously 25).
The October 2 Michelle and Brooks attempts both ended with provider no-answer,
zero connected duration, and no voicemail detection. The shorter window may
have ended the attempts before carrier voicemail answered; this is not proof
that either recipient had an available mailbox.

Keep asynchronous DetectMessageEnd for generic voicemail, wait for greeting
completion, and retain the existing single-submission guard. Longer ringing
does not guarantee voicemail delivery or authorize an automatic retry. Report
no-answer separately from voicemail submitted or delivery uncertain. The
GPT-Live voice and delegated calendar/email architecture are unchanged.
