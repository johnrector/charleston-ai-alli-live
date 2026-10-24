# Rebuild checkpoint — October 24, 2026

The clean-runtime branch replaces the old reasoning/article runtime. At this
checkpoint it has not yet been committed, pushed or deployed. Production main
is b4e05067ff8090cdadfc24dd17b408a2651d4c17. Do not confuse local tests with a
verified release. The user has authorized finishing and deployment, without
optional menus, new recurring charges or Mac-plugin migration.

## Verified

- Native API tests: GPT-Live started/finalized with PCMU 8 kHz; GPT-6.1 Sol/high
  used real public-documentation MCP tools and finished after the submitting
  client disconnected. The full configured work schema, including structured
  outcomes, web search and MCP, was accepted. Test sessions were removed.
- Actual native Mac execution wrote/read a disposable file via official
  exec-server. The separate environment-only key was denied agent session reads
  with HTTP 403. The application key was withheld. No plugin task was tested.
- Latest full local suite: 76 passed, with disposable real Postgres, authenticated
  MCP HTTP, signed transport boundaries, persistence across simulated restarts,
  receipt deduplication, caller/principal separation, contact suspension, booking
  limits, OAuth refresh continuity and callback signatures/private-address blocks.
- Native MCP Events subscription/filter/delivery/unsubscribe lifecycle passed
  against a test callback. This is not a native Dot delivery test.
- Existing Google storage was inspected: it contains an encrypted raw refresh
  token, not a Credentials JSON object. The new adapter preserves that format,
  uses existing private client registration variables and tests its compatibility.
  Current grant supports Calendar/free-busy and Gmail send, not Gmail inbox read.

## Integrated implementation

- app.py/FastAPI entrypoint; persistent shared call binding, native GPT-Live,
  durable delegation/hangup review, managed-session supervision and SMS/call
  outboxes. No TAC, application reasoning loop, step-count cutoff or article
  studio. OpenAI decides speech/tool use; Twilio only executes transport.
- Owner dashboard with persistent 12-hour sessions, CSRF, People CRUD/status,
  latest activity, work and promises. Human result links redirect to sign-in
  with a return path. Existing-code input stays visible during resend cooldown.
- Owner OAuth/MCP bridge, legacy connector refresh read compatibility, shared
  activity feed and signed MCP Events with stored subscriptions/outbound retries.
- Owner-only Mac computer assignments; signed outbound device leases with replay
  protection; official executor supervisor. Mac jobs wait offline. Plugin
  inventory is private and selectable per assignment; credentials are not copied
  into Render or model instructions. Real broker/plugin integration is untested.
- One-time useful-contact seed respects deleted/suspended/unapproved records.
  The one explicitly authorized future birthday commitment is retained once;
  old article, failed-call and other work queues are not replayed.

## Private configuration / service

Render service srv-dasnm0vpn0mc7397ejg0, confirmed workspace
tea-d9tvnp2d0e5s73a57560, existing approved 1c-2g plan. Native Python build:
`pip install -r requirements.txt`; start `python app.py`. No new service or plan.
Use MCP/API/CLI only. Database network access stays closed to external clients.

Mac files: ~/.config/alli/executor.key (restricted), device.key (Ed25519 private)
and mac.json (device/plugin inventory), all private and outside Git. The outbound supervisor is installed as the user
LaunchAgent me.johnrector.alli.executor; its real cloud handoff is untested. Public verifier and inventory were saved to the existing
Render service as ALLI_MAC_CONFIG_JSON; owner-supplied seed data is private
ALLI_SEED_CONTACTS_JSON. Environment updates used merge, preserving other values.
Do not commit private contact numbers, inventory or credentials to the public repo.

## Required next

1. Finish focused checks of the latest changes; update this checkpoint from
   actual results. Inspect git diff and commit a clean tree with rollback history.
2. Deploy through Git/MCP. Startup probes the production OpenAI key and Google
   connection, and checks for active calls before cutover. Inspect actual logs.
3. Install the outbound Mac supervisor and test the real broker, then one read-only
   compatible plugin task. API remote_url is actually https://api.openai.com;
   pass it unchanged to exec-server, which establishes its own outbound WebSocket.
4. Test real owner incoming SMS through calendar/email/delayed text, real outgoing
   human call, incoming full-duplex/hangup work and persistent phone browser login.
   Prior old-runtime outbound calls never rang John; removing AMD is not proof
   that this is fixed. Diagnose real carrier/session evidence.
5. Rescan the plugin's tools/events and verify native Dot subscription/delivery
   and the matching portal record. Do not fabricate a Dot callback or assume
   ChatGPT account credentials automatically belong to a managed API session.

Current limits: inbox reading is not authorized by the existing Google grant;
local plugins need individual connection validation. No rebuilt real human phone,
email, calendar write, delayed SMS, native Dot or iPhone login test has passed yet.
No claim of perfection or complete stability is warranted before those tests.
