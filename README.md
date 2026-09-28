# Charleston AI — Alli Live

Persistent Twilio Agent Connect service for the Tuesday real-estate demonstration.

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

The same Python service now exposes **one** OAuth-authenticated tool,
`call_contact`, at `https://charleston-ai-alli-gpt-live.onrender.com/mcp`.
It normalizes phone numbers locally and invokes the same `initiate_demo_call`
function used by `/demo-call`. The established per-call session context and
immediate greeting remain unchanged. There is no Supabase/HTTP relay, backend
LLM request, Google request, or database lookup between an authenticated tool
invocation and the Twilio dial. Structured output is `{ok, call_sid, status}`;
`queued` means Twilio accepted the call, not that someone answered.

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
The MCP tool is not idempotent: never automatically retry an uncertain response.
The original authenticated `/demo-call` endpoint remains available.

Official references checked September 28, 2026:
- https://developers.openai.com/apps-sdk/deploy/connect-chatgpt
- https://developers.openai.com/apps-sdk/build/auth
- https://developers.openai.com/apps-sdk/build/mcp-server
- https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
