# Manual release checks

Run inside Render's authenticated service shell with `PYTHONPATH=.`. No keys,
codes, connection strings or credential bundles should be printed or copied.

- `python tools/qa_database_rollback.py` exercises actual Postgres owner/call
  binding, contact lifecycle, pending cancellation and daily/weekly quotas in
  a forced-rollback transaction. Reserved fictional phone numbers are asserted
  absent first; no queued fixture is committed or visible to workers.
- `python tools/qa_phone_hangup.py --send-result-to-owner` uses the live signed
  HTTP/WebSocket bridge and real GPT-Live audio with a synthetic audio fixture.
  It makes no PSTN call, needs an existing valid owner dashboard sign-in, grants
  only its random test CallSid for two minutes, and removes the grant on exit.
  It leaves an explicitly marked synthetic call and durable real work for the
  production cloud worker. Its finished PDF is texted only to the owner.
  Check completed work, actual cached PDF and provider delivery receipt afterward.

The fixture says: “This is our synthetic cloud handoff test. Create a funny
one-page fame plan for a small business owner and text me the finished PDF.
Choose the details yourself. I am hanging up now.” It uses a synthetic local
voice as test input; Alli's output still comes from GPT-Live with marin.

Neither helper is an HTTP route, background service, authentication feature,
or automatic test. A signed bridge simulation is not a physical PSTN test or a
real multi-person meeting. Those require participants using their own phones.
