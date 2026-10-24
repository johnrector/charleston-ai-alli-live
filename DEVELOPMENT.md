# Live validation checkpoint — October 24, 2026

Native GPT-Live (gpt-live-1) owns phone conversation; native managed Agents
(gpt-6.1-sol, high) own work. Twilio transports calls/audio/SMS. The old article
studio, TAC and application reasoning loop were removed. Do not migrate the
Mac plugin collection. Ordinary cloud work needs no Mac.

## Actual deployed evidence

e12f3ec212fac80627cf744090bc1664a7cd5012 went live at 21:56 UTC, following
0a008bd54529d8c6468ff6875a0214c8a85297cf at 21:42. Native Live/Agents/MCP and
Google Calendar startup probes passed. Git preserves the old production commit
as an ancestor; no force push, new service or additional compute upgrade.

- Real incoming ALLI-CLEAN-1024 owner SMS created the correct private calendar
  reminder, sent the confirmation email, and delivered the five-minute text
  about two seconds after due time. Independent Calendar/Gmail reads verified
  results. The disposable event was deleted and its absence verified.
- The first outgoing call reached the owner but exposed incorrect Live event
  handling: offset_ms belongs to DelegationCreatedEvent, not its nested object.
  Tests now use the actual SDK model. Native follow-up inputs accept only user;
  application evidence now uses that wire role in a data envelope explicitly
  marked new_authorization=false. Server-side authority remains unchanged.
  A real corrected follow-up passed. A transient initial-input 409 retains the
  same saved key; never recreate the session as a retry.
- The second outgoing call captured a natural 55-second two-way conversation,
  finalized without a Live error and saved delegation before hangup. Alli sent
  the requested saved-contact test email after hangup; an independent Gmail
  Sent read verified its provider message ID. Other work reached the official
  Mac executor; the environment API independently confirmed connected.
- The Mac run returned verified partial results. Shopify required authentication.
  Its Gmail plugin exposed no mailbox-reading tools in this API runtime.
  Existing Calendar access worked. Do not claim all plugins/inbox reading are
  connected, or replace missing order/mailbox evidence with guesses.
- The owner confirmed an actual native Dot notification accurately reporting
  calendar/email/SMS completion and later call progress. This is real Dot
  delivery evidence, beyond local signed-callback tests.
- One previously authorized birthday SMS remains queued once for November 1,
  9 AM Eastern. Its old delivery was canceled during handover. Old article,
  failed-call and other work queues were not replayed.

## Latest reliability changes

OpenAI returned usage_limit_exceeded when the Mac result reached its parent.
The owner confirmed depleted API credits and replenished them. API credits are
separate from ChatGPT/Codex allowances. This task made no purchase or limit change.

Activity reads had copied earlier lookup results into audit events. The activity
projection now provides compact read receipts for old/new rows while retaining
raw evidence and complete write/provider receipts. Failure reporting preserves
prior progress and child results, identifies known service errors and supplies
actual write receipts to continued work. Later conversation-work results return
once to the original calling mission. Transport and completion reporting poll
independently of native session observation; a blocked AI request cannot delay
an already authorized text. Paid readiness probes are explicit diagnostics;
normal restarts do not require them. Active-call cutover protection remains.

Latest local suite: **82 passed**, with isolated real Postgres, authenticated
MCP HTTP, actual SDK schema/event validation, durable state, cryptography and
blocked-reasoner/scheduled-delivery concurrency. Check Render's commit against
Git before claiming these latest changes live.

## Remaining live gates

1. Verify the latest reliability commit through Render MCP and public health.
2. Verify the pending call mission reports actual partial results after the
   credit refill, without redialing or repeating its completed email.
3. Test a real incoming call on the clean deployment and final text after
   hangup. Earlier incoming calls were against the old runtime.
4. Confirm actual iPhone dashboard session persistence across result links.
   Twelve-hour server persistence, redirects and cooldown/code-entry behavior
   passed automated tests; that is not an actual Safari persistence test.

## Operations

Render service srv-dasnm0vpn0mc7397ejg0, confirmed workspace
tea-d9tvnp2d0e5s73a57560, existing approved 1c-2g plan. Build pip requirements;
start python app.py. Use MCP/API/CLI only. Keep the database external network
allowlist closed. Never print environment values or commit personal contact
seeds, credentials or plugin inventories.

The Mac LaunchAgent me.johnrector.alli.executor runs tools.mac_executor from
this checkout. Private credentials/configuration live in ~/.config/alli.
Its environment-only key is distinct from the application key; an actual
session-read request returned 403. Device leases use Ed25519 signatures,
timestamps and replay-resistant nonces. Pass OpenAI's returned HTTPS remote_url
unchanged to its executor, which establishes outbound WebSocket connectivity.
No inbound desktop-control port is exposed. Desktop/Alli points to this checkout.

Existing Google connector storage and owner OAuth refresh compatibility are
preserved. The Google grant supports Calendar and Gmail send, not inbox read.
Local plugin files do not automatically transfer ChatGPT account authorization.
Saved-contact recognition never grants owner-private permissions.
