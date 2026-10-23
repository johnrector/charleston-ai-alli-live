# Alli outbound operations

## Current deployment posture

John authorized production manual calling and the complete calendar/email workflow
on September 30, 2026. `OUTBOUND_CALLS_ENABLED=true` enables dialing;
`MANUAL_CALL_ACTIONS_ENABLED=true` grants mission-related calendar and email actions
to manual MCP calls by default. Set the latter false only for conversation-only
script tests. Automatic future appointment calling remains separately configured.
Secrets remain in Render and must never be pasted into chat or logs.

## Plugin API

`call_contact` keeps its existing signature, so already-connected clients gain the
production workflow without a tool refresh. Supply the verified recipient name,
phone, mission and email when known. If email is missing the caller can ask the
verified recipient for it. Supplied email cannot be substituted during the call.

`call_outbound` accepts a stable request_id. Omitted capabilities inherit the
owner-enabled manual defaults; explicit [] requests conversation only. The full
set is check_calendar, create_meeting, manage_calendar, send_email.

Calendar tools find this recipient's events, create agreed new meetings, update
existing meeting times, and cancel agreed meetings. Updates preserve the event ID
and attendees, use the event version, recheck conflicts and read back the result.
Separate email uses the connected Gmail send permission. Each action must remain
within John's mission and verified recipient agreement. No inbox-read permission
is granted by this change. A preset or raw business-card text never grants actions.

Exact repeated call_contact inputs read the existing intent, even after a manual
permission change. A database lock by recipient phone also blocks different
requests arriving within two minutes or while a call is still active (up to the
15-minute call limit plus cleanup). The blocked response gives the original ID.
Do not alter incidental fields to evade deduplication or retry an uncertain call.

Use get_call_result(request_id) for a read-only result. Refresh the plugin's tool
catalog if the client only exposes call_contact. As a compatibility fallback,
identical call_contact inputs read the existing intent without redialing.

Conversation reports are saved by the in-call tool. On disconnect, the SDK's
on_conversation_ended hook stores the available transcript if no report exists.
The fallback is explicitly labelled call_end_transcript and does not invent a
confirmed outcome or verified calendar action. Provider completed only means the
call ended. No transcript content is written to runtime logs.

Startup verifies live Calendar access and the Gmail send scope, logging booleans
only. Tests mock external mutations: no test calls or emails are sent to clients.

## HTTP operations

These routes require the existing `X-Demo-Key` administrator header:

- `POST /outbound-call`: the same body as `call_outbound`
- `GET /outbound-calls/{request_id}`: durable report and exact-call provider readback
- `GET /outbound-readiness`: configuration presence, not a live integration test
- `GET /appointment-automation/preview`: read-only eligible-candidate preview
- `POST /appointment-automation/run-once`: one gated scan; remains disabled by default

The unauthenticated `/health` exposes build commit and automation-enable status,
without contacts, secrets or mission data. Twilio callbacks and WebSocket upgrades
require valid signatures. One process/instance is required by TAC transport state.
Restart cannot clear durable claims or make uncertain calls retryable.

## Choices required before automatic activation

1. Approve the eligible event rule: explicit private opt-in tags on the owned
   primary Google Calendar. Decide who should receive each mode
2. Choose a trusted phone/contact source, verify each phone and timezone, and
   record confirmation/follow-up consent and a verification timestamp
3. Approve recipient-local quiet hours, the weekly follow-up local hour/window,
   voicemail policy, and any calendar capabilities granted to automatic calls
4. Review a dry-run candidate preview and conduct controlled real-call checks
5. Only then enable the automation loop. Manual calling has separate owner approval

## Server-owned configuration

`APPOINTMENT_POLICY_JSON` is a JSON object consumed only from server configuration.
Its defaults are disabled/unapproved. Required approval fields include `enabled`,
`approved`, `eligible_event_rule`, `verified_phone_source`, `quiet_start_hour`,
`quiet_end_hour`, and `voicemail` (`generic_message` or `hang_up`).
Confirmation defaults are `lead_minutes: 60` and `dispatch_window_minutes: 10`.
Follow-ups additionally require `follow_up_enabled: true`,
`follow_up_local_hour`, and a same-local-day window. Defaults are seven days after
the appointment's local end date and a 60-minute window. No default hour is assumed.
Follow-ups never run outside 8 a.m.–8 p.m. recipient local time.
`allowed_capabilities` defaults empty; approving it is separate from naming a mission.

`APPOINTMENT_CONTACTS_JSON` contains `approved`, `source`, `revision`, and `contacts`.
Each contact has a stable `recipient_id`, `recipient_name`, E.164 `phone`,
`phone_verified`, `verified_source`, aware `verified_at`, `recipient_email`,
`recipient_timezone`, `confirmation_consent`, `follow_up_consent`, and optional
`approved_logistics`. Directory and policy sources must match. Change the directory
revision when editing it. The configuration schema is not evidence of consent;
these attestations must come from the owner's verified source.

Eligible Google events use these **private** extended properties:

- `alli_owner_opt_in: "true"`
- `alli_contact_id: "<trusted recipient_id>"`
- `alli_confirmation_opt_in: "true"` and/or `alli_follow_up_opt_in: "true"`

Never put phones/internal missions in attendee-visible descriptions. Tags do not
verify a phone or grant consent. All-day, cancelled, declined, tentative, missing,
changed or incomplete events are skipped. Newer associated events suppress a
weekly follow-up, even without opt-in. Limits: complete 30-day lookback and 90-day
lookahead; events outside that horizon or lacking a known association are unknown.

## Verification and limitations

Run `python -m pytest -q`, `python -m compileall -q .`, and `git diff --check` before
publishing. Unit tests mock external calls; database SQL tests check transaction
contracts. Live owner testing additionally verifies the deployed commit, durable
claim/result readback, signed media/callback routing and provider disposition.

The Google refresh token may expire after seven days in Google testing mode.
Public consent-screen app information is served at `/` and the Alli-specific
privacy disclosure at `/privacy`. These static pages do not expose account data,
tokens or administration. Keep the Google Branding URLs on this same authorized
service domain. Publishing status is managed in Google Auth Platform Audience;
it is independent of Google verification and must not be inferred from a working
refresh token. Renew consent after changing from Testing to Production, then
verify encrypted durable storage, forced refresh, Calendar read and Gmail scope.
The existing free Render database expires October 28, 2026. Upgrade/migrate requires
owner approval and is not part of this change. The legacy Node sibling currently
fails on a pre-existing `AgentConnect` export error; the Python service is the
active deployment. No unverified inbound callback number is advertised in voicemail.

## One explicitly authorized call while general dialing is off

An administrator can set `OUTBOUND_SINGLE_CALL_JSON` to an object with an aware
`expires_at` timestamp and one fully validated `call` body. Its phone, recipient
name, email and mission must exactly match the incoming request. The service
uses only that server-owned body, fixed request ID and explicit capabilities;
caller-provided context cannot expand the authorization. Durable deduplication
still permits at most one dial. Expired or mismatched grants do not authorize
calls. Owner-test behavior and automatic calling remain unchanged. Remove the
grant after the requested call and result readback are complete.

## Two-way inbound cutover and rollback

Number SID: PN649ae076a3825c5c0b1e2387401866b7 (+18544447852).
Original voice: POST https://tuesday-agent-demo.netlify.app/api/callback
Original SMS: POST https://api.vapi.ai/twilio/sms
Messaging service MG528d0ca817e3c269393cf9a8a74b7cd8 defers to sender webhook.
Do not change that shared service or other phone numbers.

New voice: POST https://charleston-ai-alli-gpt-live.onrender.com/inbound/voice
Voice fallback: POST https://tuesday-agent-demo.netlify.app/api/callback
Voice status: POST https://charleston-ai-alli-gpt-live.onrender.com/inbound/voice-status
Original voice status: POST https://api.vapi.ai/twilio/status
New SMS: POST https://charleston-ai-alli-gpt-live.onrender.com/inbound/sms
No SMS fallback to Vapi: mixing agents after an uncertain delivery can duplicate
or contradict actions. Twilio retries are deduplicated by MessageSid.
Keep Vapi's old number/assistant record intact for rollback; do not save it after
cutover because Vapi may overwrite the Twilio webhooks.

Before cutover verify `/health`: exact deployed commit, inbound_worker_ready=true,
inbound_model_checks voice=true/text=true. Startup probes open a GPT-Live session
and obtain a short Responses reply without making calls or invoking business tools.
Signature tests use fixtures; live readiness is not a real phone/audio or SMS
end-to-end test. Perform owner-initiated call and text smoke tests after cutover;
check conversation, delivery and update-feed outcomes before claiming end-to-end
verification. Restore the original number-specific webhook URLs to roll back.

## Inbound name recognition

Set `ALLI_OWNER_PHONE` to the owner-confirmed E.164 mobile number. This is a
greeting preference only, not authentication or an expansion of tool access.
`alli_contact_name` stores names explicitly supplied during inbound exchanges.
Both voice and SMS use the same lookup; recent outbound mission names take
precedence and ambiguous shared numbers receive a neutral greeting. SMS uses
seven days of recent thread history. Keep GPT-Live and Responses delegation
unchanged when adjusting greetings.


## Contact directory — October 23, 2026

`alli_contact_name` is the shared cloud directory for inbound calls and texts.
`owner_saved_contact` and `owner_supplied_mission` sources enable bounded personal
scheduling and reminders; `self_introduced` is a greeting preference only.
John can manage supplied contacts through owner SMS `save_friend_contact`.
Existing unambiguous owner-authorized call recipients seed the table at startup.
Inspect `list_communication_updates.contact_directory` for policy/version and
counts, and `/health.contact_policy` for the active release.

Saved numbers are already identified for these routine phone interactions.
Do not request a second name/returning-call confirmation. Agreement to an actual
appointment or reminder is still required. SMS uses the existing Responses
model; voice uses the existing GPT-Live and delegated tools.

## New-contact approval alerts

`alli_contact_approval` holds one pending/approved/denied request per normalized
phone, a random reply code, the source inbound SID and an independent notification
delivery state. The existing inbound worker sends alerts only to the configured
owner. No extra scheduler or Mac is involved. New inbound receipts create requests
atomically; webhook retries cannot create duplicate alerts. No historical backfill
is performed. Denied numbers stay unapproved without repeated alerts.

Use exact `APPROVE code [name]` or `DENY code` SMS replies from the configured
owner. The deterministic parser bypasses the model and verifies the saved row
before claiming approval. A pending request cannot revoke a separately approved
contact. Approval applies to subsequent conversations. Alert delivery callbacks
use `/inbound/approval-status/{code}` and require the Twilio signature, account,
stored destination and matching message SID. A process interruption or send timeout
leaves an uncertain state for review, never an automatic duplicate send. The new
schema is additive and compatible with rollback to the prior application version.


## Cloud outbound SMS (October 23, 2026)

The authenticated MCP tools `send_text_message`, `get_text_message` and
`cancel_text_message` share a durable Postgres outbox with owner SMS tools.
An owner can text Alli to find a saved contact and send/schedule a message.
Unknown contacts and friends never receive these owner tools. Sending a text
does not add a contact to the pre-approved list.

`send_at` is an ISO datetime with a UTC offset; omitted means now. Resolve
John's local dates using America/New_York. An accepted schedule is not sent or
delivered. The cloud worker is independent of Mac availability and OpenAI
model health. It checks every two seconds and claims in a committed transaction
before contacting Twilio. Same request ID and payload return the original
state; changed payload under that ID fails. Interrupted or uncertain sends
never requeue automatically. A schedule expires after 15 minutes if not claimed;
Twilio queue validity is also 15 minutes.

The existing owner admin key protects POST `/outbound/sms`, GET
`/outbound/sms/{request_id}`, and POST `/outbound/sms/{request_id}/cancel`.
No new credentials or sending account are required. Requests use Alli's
existing sender and Twilio API-key client. Signed status callbacks verify the
account, sender, recipient, per-request random token and message SID; late
queued/sent callbacks cannot replace a terminal delivery status. Outgoing
texts enter the shared conversation history, so replies retain their context.
Only still-scheduled messages can be cancelled.


## Proactive appointment reminders

John authorized appointment-driven assistance on October 23, 2026. The
`appointment_reminders` policy is stored in Postgres and defaults disabled
until explicitly enabled by the owner. Authenticated MCP tools
`preview_appointment_reminders` and `configure_appointment_reminders`, owner
SMS tools, and the owner-key `/appointment-reminders` GET/POST routes provide
preview/control. A cloud worker scans every five minutes while enabled.

Default: one SMS two hours before a confirmed timed meeting, for a participant
whose invitation email identifies exactly one approved saved phone, or whose exact event has a verified Alli booking receipt bound to that approved phone. Contact
opt-outs, declined invitations, all-day/transparent events, incomplete attendee
lists, ambiguous numbers and unmatched emails suppress dispatch. The event must
be John's own event or one he accepted. No phone/name is inferred from an event
description. Early reminders falling before 8 AM Eastern move to 6 PM the prior
day; reminders at/after 8 PM move to 7 PM. No retrospective reminder is created
more than 15 minutes after its due time.

Recipients get the verified meeting time/location and an offer to help with
questions/directions. Event/contact IDs deduplicate across scans, restarts and
rescheduling. Immediately before enqueue AND before Twilio submission, the live
event, participation, contact mapping, opt-out and policy are rechecked.
Changed/cancelled events cannot send a stale queued reminder. Replies retain
the outgoing text context; Alli uses existing own-meeting lookup tools and
records factual confirmations/questions for John. `STOP REMINDERS` and
`START REMINDERS` update only the sender's preference. General Twilio STOP
remains enforced by the carrier/provider. A contact without a saved invitation email can still receive reminders for meetings they book through Alli: the successful business-tool receipt binds that exact event to their phone, without claiming their supplied email identifies them across unrelated calendar events. Other meetings need an owner-saved invitation email for automatic matching.

This feature enables proactive SMS only. Existing automatic-call policy and
switches remain independent. Failed/uncertain sends are not blindly retried or
converted into unrequested calls. Google scan failures create an actionable
communication-feed entry at most once per Eastern day.

## Owner dashboard and durable promises — October 23, 2026

`/dashboard` is the private, mobile-friendly cloud control panel. It runs inside
this existing Render web service; all records, sign-in challenges, sessions and
promise/outbox handoffs are in Render Postgres. No Mac job or local files are
needed in normal operation.

Sign-in texts a code only to `ALLI_OWNER_PHONE`, using the existing durable SMS
outbox. Codes expire after ten minutes, allow five attempts and are single-use;
requests are limited to three per 15 minutes with at least a minute between them.
Only a keyed digest is stored in the challenge table. Sessions use random tokens
stored as SHA-256 digests, expire after 12 hours and are revoked on sign-out.
Cookies are Secure, HttpOnly, SameSite=Strict, __Host-prefixed. Browser writes need
an exact allowed Origin and per-session CSRF header. The existing owner API key
can access the dashboard API via header for operations; it is never embedded in
the page, URL, JavaScript or browser storage. All dashboard routes are no-store,
noindex and deny framing. No Google OAuth scopes or credentials were changed.

Overview shows requests needing John, new-contact approvals, live primary-calendar
entries, outbox receipts, reminder policy and recent communication activity.
People edits the existing directory; saving a person explicitly approves them.
Legacy attention items may be acknowledged without deleting the original feed.

`alli_promise` is the commitments ledger, with append-only `alli_promise_event`
history. Questions require John's answer; manual tasks require his completion
note. Only owner-approved exact text is executable, through an atomic transaction
into the existing SMS outbox. The worker scans every ten seconds; tasks due and
questions needing John enqueue one daytime owner notification. Unknown contacts
continue through the existing approval flow. Direct owner answers may be entered
in the dashboard or by `ANSWER <12-character promise ID> <exact answer>` from his
signed SMS. Owner SMS also has `list_open_promises` and `answer_promise` tools.
Saved friends have phone-bound `record_promise` / `get_my_promises` on both calls
and texts. Preparation/session-focus requests can use `needs_answer=false` to
save a task for John instead of pretending a question needs an answer.

Ready answers/texts become queued, then completed only after a provider sent or
delivered receipt (the dashboard distinguishes those). Failed, undelivered,
expired and uncertain work becomes blocked and is not blindly retried. Missed
send windows older than 15 minutes require review. Queued cancellation locks and
cancels an unclaimed outbox item atomically; an already claimed send cannot be
recalled. Optimistic revisions prevent stale dashboard answers from overwriting
new work. Per-intent IDs and open-question matching prevent duplicate handoffs.

Reasoning uses `reasoning_config.py`: GPT-6 Astra, high effort. This applies to
SMS reasoning, GPT-Live's delegated Responses backend and contextual reminder
wording. The voice layer remains GPT-Live-1 with its current audio configuration.
Startup verifies both model routes. Reminder wording adds at most one contextually
useful question, informed by this contact's recent exchanges and up to 180 days
of their own prior scheduled calendar entries. Past schedule is not proof of
attendance. Calendar facts and eligibility stay deterministic and are rechecked
before/after drafting and immediately before sending. Draft failure keeps factual
wording; it does not silently switch to an older model. No scripted directions
question is required. Exact owner-scheduled messages retain their approved text.

Validation: run `python -m pytest -q`. In the cloud shell,
`python tests/check_dashboard_database.py` checks real Postgres semantics using
TEMP shadow tables and rolls all fixture data back; it sends no messages and
changes no calendars. It must run in its own process, never in the live worker.

Rollback: revert this release and deploy. All schema additions are additive and
compatible with the prior application. Reverting removes dashboard/promise
processing; durable ledger rows remain for a later fixed release. Stop/inspect any
already queued SMS separately: a deploy rollback cannot unsend a message.
