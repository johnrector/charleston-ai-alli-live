# Alli outbound operations

## Current deployment posture

General/client outbound calls and automatic calling remain disabled. Owner-only
verification uses `OUTBOUND_TEST_PHONE`; calendar actions are denied in this mode.
No real contact directory or calendar opt-in policy is installed. Secrets remain
in Render; do not paste administrator keys, API keys or OAuth tokens into chat.

## Plugin API

After refreshing the plugin's tools, use `call_outbound` with a stable unique
`request_id`, E.164 `phone`, `recipient_name`, and the approved `mission`.
`purpose` is an optional free-text label; it never grants permissions.
`preset` may be `generic`, `scheduling`, `confirmation` or `follow_up`, or omitted.
Confirm/follow-up presets require aware `appointment_start` and `appointment_end`.

- `capabilities: []` permits conversation/outcome capture only
- `capabilities: ["check_calendar"]` permits live availability lookup
- `capabilities: ["check_calendar", "create_meeting"]` permits an agreed new booking
  with the provided, validated `email`; the recipient cannot substitute another
  email. Identity and explicit agreement to the exact slot are required
- `approved_logistics` contains only shareable facts, not raw event descriptions
- `voicemail_policy` is `generic_message` by default or `hang_up` when no message
  is appropriate. Voicemail never copies raw mission/contact/appointment details

A conversation may evolve into scheduling only within its granted capabilities.
Editing/cancelling existing events, sending separate email and additional calls
are not granted. Google creation sends a Calendar invitation after agreement.
The existing free/busy recheck and deterministic event ID prevent common duplicate
bookings. Recovered bookings are reread and validated before success is reported.

Use `get_call_result(request_id)` to read status, provider disposition,
model-reported `outcome`, and separate `voicemail` submission state.
`submitted` means Twilio accepted voicemail playback instructions, not independent
proof the recipient mailbox stored it. An uncertain voicemail is never replayed.
Provider `completed` means the phone call ended; it does not prove a conversation,
attendance, booking, or voicemail delivery.

Already-connected clients can keep using `call_contact` with the original fields.
It uses the generic path with no calendar capabilities. A deterministic ID hashes
all original validated inputs, with phone normalization and blank-brief fallback.
Repeating those exact inputs reads the existing intent and cannot redial. An
intentional later call should use `call_outbound` with a new authorized request ID.
Do not vary incidental fields to evade deduplication. `/demo-call` is legacy only.

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
5. Only then enable general calls and the automation loop. Do not enable either
   merely because local tests pass

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
