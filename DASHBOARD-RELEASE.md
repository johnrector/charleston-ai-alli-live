# Alli diagnosis, people preparation and appointment workflow

GPT-Live remains the conversational layer. Client delegation preserves work after
interruption/hangup; managed Agents do research and account work. OpenAI native
Automations are the only appointment/planning clock. Existing Postgres holds CRM,
policy, intent and actual delivery evidence; no new database, dependency, service,
credentials or server permissions are introduced.

## Delivered behavior

- Dashboard is for diagnosis and resolution. Upcoming commitments remain visible;
  routine work does not require owner review. Calendar coverage shows the checked
  window, expected work versus actual native schedule/preparation/call state,
  excluded or blocked attendees and sourced reasons. Missing/unavailable/stale
  coverage is explicit. Pages only read saved data.
- Owner-authorized background preparation resolves saved identities, retrieves
  real conversations/correspondence and researches relevant public professional
  sources for unfamiliar people. Known relationships reuse evidence. Source-dated
  facts/uncertainties and observations persist in the existing CRM; no identity
  merges, approvals or sensitive inferences are authorized by these records.
- Booking waits for preparation. Outbound call intents persist while preparation
  runs and cannot dial until ready. Preferred nicknames require an explicit self
  statement from that person's saved conversation or received email. Private
  research stays owner-only; cached recipient-safe context is permission filtered.
- An inspectable, explicitly enabled appointment policy creates one native hourly
  planner, looking ahead eight days. Eligible appointments get one 11 a.m.
  recipient-local call the day before; verified Charleston meeting context can
  use America/New_York. Unknown identities/numbers/timezones, declined/tentative,
  canceled, Hachiya, test/spam, self, other-organizer and unresolved first remote
  meeting cases are excluded/blocked rather than guessed.
- Missed setup slots become catch-up candidates, never immediate deployment calls.
  A separately authorized catch-up must be daytime today, at least 15 minutes
  ahead, for tomorrow's appointment. Each event/attendee retains one durable intent
  across revisions and native invocations. Existing manual/conditional and recent
  attempted confirmation evidence prevents duplication.
- Pre-dial checks re-read the source event, endpoint/status, preparation binding,
  mailbox/conversation freshness, policy, time window and prior receipts. An atomic
  claim prevents another attempt. Cancellations persist a stop before modifying
  the native schedule. Source changes after queueing hold the old call safely.
- Calls use source-derived time/place and a bounded confirmation/questions mission;
  brief voicemail, no automatic redial, financial terms, rescheduling or promises.
  Native invocation completion, carrier status and actual dialogue remain separate.

## Latency and persistence

No web research/model work runs in Live startup or audio handling. The existing
call read can select a cached evidenced greeting. Optional relationship context
starts after session.started, with an 80 ms budget, 50 ms SQL timeout and 480-byte
quiet thinking projection. Private owner notes do not enter contact conversations.
Blocked-provider tests verify audio proceeds; they are not production latency
benchmarks.

Research maintenance, planner work, general reasoning, call preflight and SMS have
independent I/O loops. Planner execution leases recover interrupted native tasks;
leases do not determine appointment times. Durable preparation waiters replay with
stable identities and isolate canceled requesters. Additive migrations are
repeatable, with no destructive backfill/down-migration; old code can ignore the
new CRM/preparation/intent/coverage records.

## Validation and deployment

The first dashboard/context release is production commit
63329f010701e6d35b51f7f48e255e343adf69ff, verified live October 31 at 15:51 UTC.
The active Python Render service is srv-dasnm0vpn0mc7397ejg0. The separate legacy
service build failure is not an active-service outage.

The workflow release has explicit user deployment/configuration authorization.
Independent read-only review found no remaining confirmed deployment blocker.
The final regression run passed 227 tests before the source-derived mission and
coverage layout finishing edits; the final workflow/application/worker checks also passed 40 tests after those
finishing edits. Tests use disposable local Postgres, authenticated MCP HTTP and fake
Google/native/carrier providers. They cover DST, restart/single-connection pooling,
permission isolation, nickname sources, pending booking/call continuation,
waiter replay, manual deduplication, source revisions, cancellations and fresh
pre-dial holds. Dashboard views were visually inspected with fictional records.

After pushing, verify exact Render/public release, authenticated dashboard, saved
policies, native planner and actual preparation/schedule receipts. Never use an
appointment call as a smoke test. The once-daily diagnostic claim and schedule
remain unchanged. Production contact/call receipt IDs belong in the private
handoff, not committed seeds.

## Official design references

- https://developers.openai.com/api/docs/guides/live-delegation
- https://developers.openai.com/api/docs/guides/agents-api/sessions/automations
- https://developers.openai.com/api/docs/guides/agents-api/sessions/events
