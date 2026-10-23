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
SMS uses `/inbound/sms` and the same reasoning model. Calls and SMS share the
durable `alli_contact_name` directory in the existing Render Postgres database.
Owner-saved contacts and unambiguous contacts from owner-authorized calls receive
a first-name greeting and standing assistance for their own appointments,
messages and reminders. Do not require them to repeat their identity or continue
an old mission. Historical mission date windows do not limit new requests.
Unknown or ambiguous callers receive a generic greeting and can leave a message;
a self-introduced name is remembered for greetings but grants no appointment access.
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
John is already recognized on both voice and SMS; never ask him to confirm
his name or whether he is returning his own call. Owner SMS keeps its existing
calendar lookup and requested-email tools plus contact saving and reminders;
owner voice can save requests but cannot place outbound calls. Ambiguous shared numbers receive a neutral greeting.

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

## Saved contact assistance — October 23, 2026

This supersedes the earlier identity-confirmation requirement for saved contacts
on inbound voice and SMS. Numbers are normalized to E.164; US ten-digit, leading
1 and formatted numbers resolve to the same record. No new database/service is
needed. Startup adds an optional invitation email and seeds unambiguous contacts
from prior owner-authorized missions. Owner-saved records take precedence.

John can text: “Save Jane Doe, 8435550100, jane@example.com as a friend.” The
owner-only `save_friend_contact` tool stores the supplied number/name and optional
email. A caller cannot approve themselves or overwrite an owner-approved name.
Shared-number conflicts stay generic until John supplies a specific contact.

Saved contacts may check availability, book agreed meetings, find/move/cancel
only their own meetings, and leave a message. No repeating name/returning-call
question is needed. Confirm missing appointment details, check live availability,
and preserve the existing event when moving it. Invitations use the saved email;
if no email is saved, ask the recipient for it. Existing tool receipts, duplicate
protection and uncertain-write reporting remain in force. Busy blocks and unrelated
event details are excluded from contact availability responses.

`create_reminder_for_john` saves a private, transparent five-minute event on John's
primary calendar with a popup at the agreed time. It does not text/email John;
notification delivery depends on his Calendar settings and is never guaranteed.
Untimed messages use the durable communication feed and do not promise a response.
The feed also reports directory counts by source for deployment verification,
without publishing numbers/names in the public health endpoint.

The GPT-Live voice model, delegated reasoning model, outbound calling behavior
and voicemail timeout remain as deployed. Roll back with a revert of this release;
the added email column and seeded directory rows are compatible with the prior
code, though the prior identity loop would return. No calls/messages/calendar
changes are made merely by deploying or running the mocked regression suite.

## Conversational availability — October 23, 2026

On both saved-contact calls and texts, answer exact-time questions with
`check_meeting_time`, which returns an explicit yes/no for the entire interval.
When no duration is given, check one hour and say so. Resolve weekday dates from
current Eastern time and say the actual date. Never answer from remembered
availability, infer a conflict from missing office-hour suggestions, or request
an invitation email before answering a simple availability question. If busy,
offer verified alternatives without disclosing event titles, attendees or busy
blocks. Use `check_calendar` for broad windows and follow-up changes.

An availability inquiry is not a booking instruction. Once the recipient asks
to reserve the time and the date/time/duration/email are known, use the existing
meeting tool, which rechecks conflicts before writing. Preserve conversation
context and do not restart identity confirmation. Claim booked only after the
verified event ID.

The current cloud source is John's Google primary calendar, also displayed in
Apple Calendar. Separate iCloud calendars are not included. An iCloud Calendar
integration is available but was not connected during this release; native Mac
Calendar visibility is not a cloud authorization for the Render service.

Only saved/pre-approved contacts may check availability and book. New callers
and texters should hear: “Since you're not on John's pre-approved list, I'll
need his approval before I commit to a time.” Collect their name, contact
information and preferred date/time, save the request for John's review, and
continue naturally without an identity loop. Do not claim John was contacted,
promise a response time, or approve the request yourself.

## Apple Favorites seed — October 23, 2026

The initial Apple Phone Favorites snapshot contains 50 favorite actions, which
consolidate to 48 phone numbers. Import through `import_apple_favorites` using
an owner-authorized snapshot. The atomic merge validates all rows before writing,
normalizes numbers, rejects conflicting names, preserves explicit owner-saved
corrections, and verifies the resulting rows. Source `apple_favorite` is a
pre-approved contact on both voice and SMS. Existing saved contacts are retained.
Do not commit the private snapshot to the repository.

Twenty-five numbers have display names; twenty-three favorite entries show
only numbers. Those numbers still have standing appointment access and get a
generic greeting. Obtain an explicit name once before booking or accessing
personal meetings, remember it while retaining the approved Favorite source,
and use that name in the current conversation's action tools.

This is an initial seed, not continuous Apple synchronization. A later owner
request to refresh from Apple Favorites should obtain a complete fresh snapshot
through the Phone UI (including call and message entries), then merge it through
the same import function. Removing a favorite does not automatically revoke an
existing approved contact. Owner SMS `save_friend_contact` remains available for
individual additions or corrections without editing a database table.

## New-contact approval by SMS

New incoming callers and texters queue one durable approval alert per phone
number to `ALLI_OWNER_PHONE`. Existing approved contacts, the owner, hidden caller
IDs and Twilio opt-out control messages do not trigger alerts. Voice waits until
the interaction ends or two minutes; SMS normally waits until its response is
processed so introductions and request previews can appear in the alert.

John replies `APPROVE <8-character code>` (optional contact name after the code)
or `DENY <code>` to Alli. Exact commands in the current signed SMS from John's
configured phone are parsed before the model. Quoted caller text, conversation
history, a bare "yes", or someone else's phone cannot approve a request. Approval
atomically saves the number as `owner_saved_contact`; the next call or text gets
saved-contact assistance. An unnamed approved number may check availability,
but must introduce themselves before booking. Denial leaves the number unapproved
and suppresses repeat approval alerts. Saving a contact separately still works.

Notifications persist a sending state before dispatch, track signed delivery
callbacks, and never blindly retry uncertain sends. Failures are recorded in the
owner communication feed. Approval is not a booking and does not send an extra
message to the new contact or change a calendar event.


## Owner-authorized texts and schedules

Use `send_text_message` for John's authorized SMS, with verified recipient,
exact message and stable request_id. `send_at` includes its UTC offset; omit
for immediate dispatch. Schedules persist in Alli's Render Postgres and run
with the Mac off. Read `get_text_message` before reporting outcomes or
retrying; scheduled/queued is not delivered. Cancel an unsent schedule with
`cancel_text_message`. Never change IDs to bypass an uncertain attempt.
John can also ask for these actions directly by SMS to Alli; named recipients
are resolved from the existing trusted directory and ambiguous numbers require
clarification. A new outgoing text does not authorize its recipient to access
John's calendar.
