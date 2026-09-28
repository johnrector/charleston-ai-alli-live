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
