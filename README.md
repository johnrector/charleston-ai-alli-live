# Alli — John Rector's executive assistant

OpenAI GPT-Live provides full-duplex phone conversation. OpenAI's managed Agents
API runs GPT-6.1 Sol with high reasoning for delegated work. Twilio transports
audio, calls and SMS. Postgres retains relationships, assignments, commitments
and actual provider receipts. Ending a conversation never cancels its work.

The private `/dashboard` shows people, work, promises and activity. Owner sign-in
uses a phone code and a persistent 12-hour session. The authenticated `/mcp`
endpoint connects Dot to the same work records and provides MCP Events.

Ordinary cloud work needs no Mac. Authorized local work waits for the Mac mini's
outbound supervisor and uses OpenAI's official `codex exec-server`, with a
separate environment-only key. Compatible plugins load from their existing Mac
directories. Connected-account credentials are not assumed to transfer from
ChatGPT. Friends cannot use John's computer or private account tools.

## Run and test

Install `requirements.txt`, configure the service's existing private environment
variables, and run `python app.py`. `/healthz` reports configuration. Startup
checks Google Calendar and refuses cutover while a call is active. Native model
configuration is verified on each dispatch. Run `python -m tools.probe_native_openai`
for a paid diagnostic of the actual Live/Agents connections; setting
`ALLI_RUN_STARTUP_PROBE=true` also requires that diagnostic before cutover.
Normal restarts do not launch paid reasoning tests or stop scheduled delivery
because an unrelated AI-provider spending limit was reached.

Use `.venv/bin/python -m pytest -q` for the isolated test suite. Database tests
create disposable Postgres clusters and never use production `DATABASE_URL`.

On the configured Mac, run `.venv/bin/python -m tools.mac_executor`. Its private
configuration and credentials live in `~/.config/alli`, outside Git. It exposes
no inbound desktop-control port and never passes the application API key to
the executor.

See [ARCHITECTURE.md](ARCHITECTURE.md) for boundaries and verified OpenAI sources,
and [DEVELOPMENT.md](DEVELOPMENT.md) for the current release evidence and limits.

## Owner daily diagnostic

The owner-only MCP tool `check_service_health(request_id)` accepts
`daily-health:YYYY-MM-DD`, using the current date in `America/New_York`.
It runs only when invoked: there is no timer, startup probe or conversation gate.
One durable claim per local day prevents additional attempts after failure,
timeout, interruption or a changed request ID. Repeating the same ID reads the
saved result; an interrupted attempt returns `uncertain` rather than restarting.
Previous dates can be read but cannot start new probes. The external owner
automation is responsible for the single 3 a.m. Eastern invocation.

The check opens/closes a silent Live session, makes one no-tool Responses request
(maximum 256 output tokens, high reasoning), and reads an existing owner Agents
session. It uses existing credentials with retries disabled and bounded timeouts.
No normal work task, notification, call, SMS, email or business action is created.
Agents access is read-only, not a new work-dispatch test. Results do not establish
telephone-carrier delivery, private-account tool health or future availability.
