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

Inbound voice failure uses `/inbound/fallback`, which gives a brief text-back
instruction and ends the call. Never forward untrusted incoming callers to
John's personal number. The phone number's VoiceFallbackUrl must match this
safe route; deployment verification checks that transport configuration.
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


## Appointment-driven assistance

When the owner enables proactive reminders, Alli checks the live calendar in
the cloud, matches approved contacts by unique saved invitation email, sends
one timely reminder, and offers help with directions/questions. Use
`preview_appointment_reminders` for a read-only preview and
`configure_appointment_reminders` for an explicit owner enable/disable request.
Default is two hours before a confirmed meeting, with 8 PM–8 AM Eastern quiet
hours, recipient opt-outs, fresh calendar checks and durable deduplication.
Do not claim every saved phone can match every calendar event automatically: use a saved invitation email or a verified Alli booking receipt binding that exact event to the approved phone. This does not activate automatic calls.

## Private dashboard, promises and contextual reasoning — October 23, 2026

John's private control panel is `/dashboard` on the existing cloud service.
It uses owner-phone one-time codes and revocable sessions. Calls, texts,
calendar/email work and promises all continue while John's Mac is off.

When an approved friend asks Alli to get an answer from John, use
`record_promise` before promising later follow-through. The phone is bound by the
server; a model cannot substitute another recipient or approve its own answer.
Use `get_my_promises` for continuity, avoid duplicates, and do not promise a
response time. Session preparation/focus supplied by the friend can be saved as
`needs_answer=false` so John receives the context without inventing an answer.
Only John can supply the outgoing answer, complete a manual task with a note, or
cancel open work. Owner SMS can inspect `list_open_promises`, use
`answer_promise`, or reply `ANSWER <promise ID> <exact answer>`. Queue acceptance
is not delivery. Errors/uncertain sends remain visible and must not be resent
blindly. A saved request is not a booking or proof that John agreed.

Use context and judgment, not a script or checklist. A repeat visitor may need a
session-focus question rather than directions. Do not re-ask questions already
answered, assert attendance from a past calendar item, or fabricate familiarity.
The reminder worker reads the bound contact's own calendar/conversation context
and drafts one useful optional question; verified appointment facts, permissions,
quiet hours, opt-outs and send deduplication remain enforced by code.

The selected reasoning backend is GPT-6.1 Sol with high reasoning for text,
contextual drafts and delegated call actions. Keep the existing GPT-Live voice
layer. Current official model guidance and live project probes were checked
before selecting this combination. Further upgrades still follow the policy at
the top of this file; do not silently fall back to an older low-reasoning model.

## Cloud work by call or text — October 24, 2026

Approved contacts can delegate research, writing, analysis and real artifacts
using `start_cloud_work`, check `get_my_work`, supply missing information with
`continue_cloud_work`, and cancel unfinished work. Work persists in Postgres;
ending a call or putting the phone down does not stop it. The hosted executor
uses GPT-6.1 Sol with high reasoning, current web search and hosted shell skills.
It retrieves actual files into durable storage, returns a expiring 7-day result
link by the existing SMS outbox, and tracks result-message delivery separately.
Generated, partial, blocked, queued, sent and delivered are distinct states.

John's signed owner SMS also has verified-directory lookup, explicit requested
outbound calls and call-result lookup. Calls retain the existing durable
recipient/idempotency safeguards. This does not enable unsolicited appointment
calls or let friends call third parties through John's account.

The cloud app gateway runs pinned official Codex 0.164.0 over private stdio on
Render. It uses a separate owner-authorized ChatGPT device login, an isolated
profile, encrypted credential storage, and a cross-instance token-rotation lock.
It runs no Codex model turns and exposes no network app-server port. Connected
app tools are discovered from the authenticated account. Mac-only MCP servers
and native application sessions are not inherited or relayed through the Mac.

Owner work may use connected account tools. An independent high-reasoning
check allows routine actions already authorized by the original assignment
without asking again. It pauses for genuinely missing authority or a required
provider consent step. Exact-action receipts precede account execution, and
uncertain actions are never replayed. Friend work has a hard server-side
boundary excluding John's account tools; requests needing his private data or
decisions ask him, while independent research and file work proceed normally.

The private dashboard now includes Work and Tools & skills. The private skills
snapshot imports enabled workflows and resources, not passwords or desktop
sessions. Availability of instructions does not make a missing tool usable.
Use a genuine cloud equivalent or report the missing dependency honestly.


## Delegation, group calls and owner controls — October 24, 2026

Alli's purpose is finished work. Treat a clear imperative as an assignment,
including a playful one. Pick sensible details, persist the job, continue after
hangup, and text the actual result or blocker. Ordinary accepted work does not
require another confirmation. A joke alone is not execution. Do not pretend a
plan made someone rich/famous or paid their taxes.

OpenAI GPT-Live-1 controls the full-duplex conversation and delegation timing.
GPT-6.1 Sol with high reasoning handles backend work. Twilio carries PSTN/audio
and SMS. Do not replace this architecture with Twilio conversational products,
manual turn detection or a different OpenAI voice family. The official Live
prompt uses Backchannel policy, Interruption policy, Delegation policy and
Generate <|SpawnThinking|>. Detailed business rules belong in the backend.

The pinned TAC provider extension observes the existing primary OpenAI event
stream to retain transcript fragments/timestamps, audit backend work and enforce
budgets; it does not take over listening or speaking. Voice transcript fragments
and work progress checkpoint in Postgres. Post-call recovery reviews only calls
started after the rollout, respects existing jobs/tool receipts, and recovers
unfinished delegated assignments. Its review has no tools or notification;
actual child jobs execute and notify. A shared line never gains owner-account
permissions through hangup recovery. No old calls are replayed at deployment.

When a group is introduced, OpenAI triggers group-note mode, announces notes,
and listens without competing. Record explicit participants/assignments; mixed
phone audio does not establish each speaker's identity. Keep group notes
separate from any earlier private preamble. Pause/clear notes on objection.
Requested action-item texts contain only the bound approved participant's items,
with a specific offer of feasible help. Being listed is not itself delegation.
Unknown recipients and unclear owners go to John, without guessed delivery.

Names/phone matches personalize greetings. Private owner actions require a
12-hour verified SMS session or a grant bound to that specific inbound CallSid.
A verified dashboard login also verifies owner texting. A shared call has narrower
permissions even when John's number originated it. Saved contacts keep natural
availability/new-booking help; existing private meeting/work details on voice
require the carrier's A attestation or a text from their saved number.

Phone account tools use an explicit Google productivity allowlist; installed
hosting, financial, security-admin and public-sharing tools are outside it.
Original assignments and real sender clarifications constrain recipients/actions.
Friends have only their own bound meetings, messages and public/file work.
Meeting ownership requires saved invitation email or an exact owner-approved
phone/event binding; a caller's name never grants access. Shared meetings with
other guests require John’s review before a participant can change them.

Default saved-contact ceilings: 4 calendar changes/day, 8/7 days, 3 upcoming
meetings within 90 days, 5 cloud jobs/day, and 3 active jobs. Additional call,
message/segment, model and global limits apply. Calendar suggestions default to
daily 9–5 Eastern; actual availability always comes from the live calendar.
Suspension cancels pending jobs/messages and blocks new access. Delete moves
the contact out of the active directory while retaining a recoverable profile,
access tombstone and owner activity. The owner can restore it from Deleted.
Already-dispatched external actions remain subject to their actual receipts.

The authenticated owner Activity view searches/paginates all retained updates,
opens conversations and work receipts, and provides contact lifecycle controls,
result-link revocation, session controls and an editable shareable profile.
Owner account-work results require owner sign-in. Friends' result links remain
accountless, expire after 7 days, and are revocable. Cached binaries are retained
for 14 days; textual results/history remain in the database.

Basic public facts are seeded from John's site and ai-chs.com, separately from
private account data. The profile includes who Alli/John are, lab address, hours,
rates and booking/payment basics. Answer supplied facts conversationally; use
real delegation for live availability, sending, changes and actual work.
Say "Checking" only with real backend work. Recheck time-sensitive public facts
when the profile is older than 30 days. Do not turn a personal meeting into a
paid service merely because the lab's published rates are known.

## Durable phone assignments and owner controls — October 24, 2026

Phone-delegated calls now enter the persistent cloud work queue. The cloud worker
uses the existing GPT-Live dispatcher, waits for its actual outcome, and delivers
the result without depending on a live call or Mac. Request IDs prevent redials.
Explicit future calls can wait up to 14 days without occupying a worker lane.
Friend requests to call another person require an exact action approval; a
verified saved friend can explicitly request a callback to their own number.
Calls never authorize payments, deposits, purchases, security changes or new
financial/legal commitments. Twilio remains transport; OpenAI owns conversation
and reasoning. Account tools exposed to phone work remain Google productivity
only; installed connections do not grant blanket phone access. Private skill
snapshots are mounted for owner work only, never friend or group jobs.

People is the owner contact manager: add/edit, suspend/reactivate, recoverable
delete/restore, conversation/activity sorting, per-person calls/texts and work
receipts. Suspension cancels pending work and unsent scheduled messages; restore
does not replay them. See Desktop/Alli/RELEASE NOTES.md for deployed evidence.
