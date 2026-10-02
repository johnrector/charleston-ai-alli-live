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
