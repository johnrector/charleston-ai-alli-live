# Charleston AI — Alli Live

Persistent Twilio Agent Connect service for mission-driven outbound calls and the original real-estate demonstration.

Architecture:

Twilio Agent Connect → OpenAI GPT-Live-1 → delegated reasoning with GPT-5.6 Sol.

Foreground context keeps John identity, properties, Eastern Time, recipient identity, and current mission at conversational latency.

## Google integration

The Python service is `charleston-ai-alli-gpt-live` (not the legacy Node service).
`GET /google/authorize` presents an administrator-key form, or starts OAuth when
called with `X-Demo-Key`. The callback is exactly:
`https://charleston-ai-alli-gpt-live.onrender.com/google/callback`.

Configuration: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`GOOGLE_OWNER_EMAIL`, `GOOGLE_TOKEN_ENCRYPTION_KEY` (Fernet), and `DATABASE_URL`.
Keep existing Twilio/OpenAI settings and `DEMO_KEY`. Secrets belong only in Render.
The OAuth flow uses offline consent, PKCE, a secure HttpOnly cookie, expiring
single-use server-side state, and verified Google account identity. It stores
only the refresh token, encrypted in private-network Postgres. No local token
file is used. A fresh access token is obtained per operation.

Scopes: Calendar free/busy, events on owned calendars, Gmail send, and
OpenID/email identity to restrict connection to John's account. No inbox read,
Drive, or unrelated data permission is requested.

`GET /google/status` with `X-Demo-Key` reads the committed token, forces a
refresh, checks real primary-calendar availability and verifies the Gmail-send
grant. It does not send email. `POST /google/self-test` with the same header
creates, reads and deletes a transparent test event without guests or reminders.

Testing-mode Google refresh tokens expire after seven days. Reconnect before
a later demo if needed. The initial free Render database expires October 28,
2026; upgrade or migrate before then for continuing use. Keep the Fernet key
with the database; rotating it without re-encrypting the token loses access.

## Per-call request

Authenticated `POST /demo-call` accepts the existing `to`, `name`, and `brief`,
plus `recipient_name`, `email`, `company`, `title`, `business_card_text`, and
`mission`. `to` is the recipient phone in E.164 form. `mission` overrides `brief`.
Each request uses TAC 2.5's `InitiateVoiceConversationOptionsGPTLive.session_config`
and a deep copy; no shared channel configuration is changed. TAC correlates the
context before dialing and consumes it before `session.start`. Keep one Render
instance for this demo because TAC's pending call transport context is in memory.

All Charleston scheduling is America/New_York. Sol executes `check_calendar`,
`create_meeting`, and `send_email`. Calendar creation rechecks conflicts and uses
a deterministic event ID for retry safety. Gmail sends record pending state
before transmission; an uncertain send is never automatically resent. Repeating
identical recipient/subject/body returns the existing result.

Run local tests with `python -m pytest -q` (install pytest separately).

## ChatGPT native calling (MCP)

The same Python service exposes OAuth-authenticated calling and result tools at `https://charleston-ai-alli-gpt-live.onrender.com/mcp`.
`call_contact` accepts the original contact/mission fields but now uses the
generic isolated call path. `call_outbound` adds explicit capabilities, optional
presets and request IDs; `get_call_result` reads durable outcomes. The new path
commits a database deduplication claim before dialing. `queued` means Twilio
accepted the call, not that someone answered. See the general-purpose section below.

Connect in ChatGPT → Plugins → Add → Create MCP App. Use the MCP URL above,
OAuth, and the private connection name **Charleston AI — Alli Calls**. Discovery
provides authorization/token/registration endpoints and the `calls:write` scope.
The one-time owner approval form is served by this backend. Enter the existing
administrator key only in that password form, never in a chat or tool argument.
ChatGPT receives scoped OAuth tokens, never `DEMO_KEY`.

OAuth uses the maintained official MCP Python SDK 1.30, S256 PKCE, exact registered
ChatGPT callback URLs, confidential client authentication, one-use codes,
rotating refresh tokens, and issuer/audience/scope/expiry checks. Client secrets
and refresh records are encrypted in a separate table in the existing private
Render PostgreSQL database. Access tokens expire after five minutes; revoking a
refresh token prevents further refresh immediately, while an already-issued
access token can remain valid until its five-minute expiry. Rotating `DEMO_KEY`
invalidates all current access tokens. Keep the database and its encryption key.

Use the connected plugin in the demo chat. Establish the mission before sending
the photo, for example: “For the next card, call the contact immediately. I'm
selling my Mount Pleasant house and want to meet about representing me. Read the
card silently, invoke call_contact, and after acceptance say only Calling.”
Tool metadata and server instructions reinforce that flow; truthful write and
open-world annotations remain enabled. ChatGPT controls permission prompts and
model/vision latency, so no server can guarantee zero prompts or <10-second
photo-to-ring latency. Measure that separately from server-to-Twilio acceptance.

Render logs record `outbound_call_accepted` and `mcp_call_accepted` with Call SID
and elapsed milliseconds, without contact text, mission text, keys, or tokens.
General calling claims are idempotent; never create a new request ID merely to
retry an uncertain response. The original authenticated `/demo-call` endpoint
remains non-idempotent and must never be retried automatically.

Official references checked September 28, 2026:
- https://developers.openai.com/apps-sdk/deploy/connect-chatgpt
- https://developers.openai.com/apps-sdk/build/auth
- https://developers.openai.com/apps-sdk/build/mcp-server
- https://github.com/modelcontextprotocol/python-sdk/tree/v1.x

## General-purpose outbound calls (disabled by default)

The reusable path is `call_outbound` (MCP) or administrator-authenticated
`POST /outbound-call`. The existing `call_contact` now forwards to the same generic, no-calendar
mission path so already-connected plugins do not inherit the demo.
`/demo-call` alone remains the explicit legacy real-estate demo. New calls **do not inherit the demo instructions,
property addresses, business-card text, or its executable tools**.

A call is a **mission + bounded context + explicit capability envelope**.
`purpose` is an optional descriptive label; presets provide optional starting
points for confirmations, scheduling and follow-ups. They never grant actions.
The conversation can evolve naturally without redeployment. For example, an
appointment follow-up may turn into scheduling if `check_calendar` and
`create_meeting` were explicitly authorized for this call. With no calendar
capabilities, the same request is captured for John instead. Sending email,
editing/cancelling events, making other calls and enrolling anyone in future
calls are not available in this path.

Use a stable `request_id` for one intended call, including every retry/readback.
The compatibility `call_contact` derives its ID from normalized original inputs;
repeating those exact inputs returns the existing call/report without dialing.
To intentionally call again later, use `call_outbound` with a newly authorized
request ID. Compatibility calls never gain calendar powers from their mission
text; refresh the plugin tools and use explicit capabilities for scheduling.
Reusing it with different details is rejected. A durable PostgreSQL claim commits
before Twilio is invoked; existing claims never dial again, including a crash,
network timeout, or uncertain response. Deliberately making another call needs a
new authorized intent and request ID, not an automatic retry with a new UUID.
`get_call_result(request_id)` / `GET /outbound-calls/{request_id}` returns dispatch
status, provider disposition, and a structured conversation report if one was
saved. `queued` only means acceptance. `completed` only means the phone call
ended; neither means the person attended or that an appointment was booked.
A missing outcome remains missing, never synthesized into success.

Each call has its own TAC channel, executable tool registry and server-bound
outcome closure. The model cannot choose another call's outcome ID. Booking
capability exposes a recipient-bound wrapper, with verified identity and explicit
agreement to exact time/duration/timezone required before the existing Google
booking path runs. The existing Google free/busy recheck and deterministic event
ID remain in force. Recipient email must already be supplied and validated;
unknown email blocks booking instead of allowing arbitrary recipient injection.
Calendar invitations are sent by Google creation; no separate email is sent.

All new webhook and WebSocket routes validate Twilio signatures. Uncorrelated
connections cannot fall back to the demo profile. Answering-machine detection
requests hang-up on machine/fax/unknown; the assistant first asks identity and
must not disclose call details before identity is confirmed. AMD can classify
late or incorrectly, so actual behavior still needs an approved real-call test.
Calls time out at 15 minutes; unanswered ringing times out at 25 seconds.
This process retains transport state in memory, so **keep one service process
and one instance**, as with the original TAC demo. Restart loses active/pending
transport context but does not make the durable call claim retryable.

### Activation checklist

Nothing in this change enables calls, starts a schedule, deploys, or changes
Google grants. `OUTBOUND_CALLS_ENABLED` defaults to false. For an explicitly approved owner-only
test, keep it false and set `OUTBOUND_TEST_PHONE` to that one verified E.164
number. This mode rejects every other recipient and all calendar capabilities;
it does not activate a scheduler. Remove the test setting after validation. `GET /outbound-readiness`
requires `X-Demo-Key` and describes configuration and automatic-call blockers;
it is not a live connectivity test. Use existing credentials, private Postgres,
and the existing OAuth connection; no new credential grant is implemented.

Before enabling manual calls: deploy reviewed code only with owner's approval,
verify schema creation/atomic claims against real Postgres, check the existing
Google token/database lifetime, verify public signed routes on the actual domain,
and conduct an explicitly approved call to a verified test recipient. Exercise
answer, wrong person, voicemail, no answer, questions, agreed booking, and readback.
Verify call timing, sound/latency, identity-first privacy, per-call tools, callback
ordering and prompt behavior. Local tests substitute all external side effects;
they do not establish production readiness.

Before automatic confirmations or weekly follow-ups: settle eligible-event rules,
verified contact/phone source and consent, recipient timezone/quiet hours,
voicemail policy and retention; connect a read-only calendar instance iterator and
fresh event/contact revalidator. `appointment_confirmation.py` provides a dormant
approximately-one-hour dispatch-window checker and callback-based dispatcher,
with cancellation/reschedule/contact-change revalidation before dialing. It is
not a running watcher. Weekly selection must identify the recipient's latest
relevant appointment and avoid later/cancelled/replaced events; that selector and
scheduler are deliberately not installed. Select an approved schedule only after
those adapters/policies and live integration are verified. Never point a timer at `/demo-call` or bypass policy gates with manual-call tools.

Local checks: `python -m pytest -q` and `python -m compileall -q .`.
New ledger tests verify SQL transaction contracts with mocks; a real PostgreSQL
concurrency/commit test remains part of pre-production validation.
