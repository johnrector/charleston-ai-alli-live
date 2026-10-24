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
variables, and run `python app.py`. `/healthz` reports configuration; startup also
checks the actual OpenAI Live/Agents connections and Google Calendar before the
service accepts traffic. Deployment refuses cutover while a call is active.

Use `.venv/bin/python -m pytest -q` for the isolated test suite. Database tests
create disposable Postgres clusters and never use production `DATABASE_URL`.

On the configured Mac, run `.venv/bin/python -m tools.mac_executor`. Its private
configuration and credentials live in `~/.config/alli`, outside Git. It exposes
no inbound desktop-control port and never passes the application API key to
the executor.

See [ARCHITECTURE.md](ARCHITECTURE.md) for boundaries and verified OpenAI sources,
and [DEVELOPMENT.md](DEVELOPMENT.md) for the current release evidence and limits.
