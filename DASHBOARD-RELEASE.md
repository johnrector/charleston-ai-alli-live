# Owner dashboard and relationship-context release

This release keeps GPT-Live as the conversational front end, durable client delegation as the work boundary, and OpenAI native Automations as the only future-work clock.

## Behavior

- Today shows upcoming saved commitments, pending/unconfirmed call attempts, recent work to review, and the latest cached AI diagnostic.
- People retains the address book and per-person activity, and adds owner-private observations with their saved evidence and dates. The delegated-owner tools `relationship_context` and `remember_relationship` read/save notes; corrections preserve the source and earlier note. Friends gain no new tools or access.
- Rules displays current operating-rule records from the existing collections, including source and revision. Appointment confirmation is explicitly **not configured**. There is no enable button, calendar scanner or automatic call activation.
- Calls & work distinguishes the schedule state, each run's work outcome, actual carrier status and transcript finalization. Active schedules and pending commitments precede historical items, so old history cannot hide them behind the display limit.
- Health reads the saved diagnostic. Visiting the dashboard never starts a paid probe, communication or delegated assignment.
- Activity retains raw receipts under expandable details. Existing sign-in, CSRF, owner authorization, contact approval and connection routes remain intact. No JavaScript or new dependency was added.

## GPT-Live path

The optional relationship lookup starts **after** `session.started`, outside audio handling. It has an 80 ms application budget and a 50 ms SQL statement timeout, and failures are skipped. It returns compact metadata through a quiet `session.thinking.append`, capped at 480 UTF-8 bytes. Contact calls never receive owner-private notes; unverified calls receive no relationship brief. No model or external account query is added to voice startup.

The blocked-lookup test proves that Live starts and audio plays before the lookup completes. This is a behavioral test with a fake blocked provider, not a production latency benchmark. Real caller latency/audio remains a post-deployment validation gate.

## Data and rollout

`ea_relationship_note` is one additive, initially empty table in the existing database. Composite foreign keys bind it to the same assistant/person and actual task/conversation evidence. There is no backfill, contact approval change, new database or new account access. Startup initialization is repeatable. Rolling back application code can leave this unused table in place; no destructive down-migration is required.

Notes are observations, not automatically verified facts. Owner-authorized workers can save them after substantial work; this release does not run a new transcript-mining agent or import old personal data. A full autonomous CRM-maintenance workflow and structured calendar-to-call planning remain follow-on decisions.

The full regression run exposed a pre-existing concurrent daily-diagnostic claim race between its day and request-ID unique indexes. `ON CONFLICT DO NOTHING` now handles either index, preserving the existing once-per-day claim and uncertain-outcome behavior. No extra diagnostic is scheduled.

## Verification

- Baseline: 190 tests passed with a disposable local Postgres cluster.
- Release: **204 tests passed** in the full regression suite, including real Postgres/MCP HTTP, permissions, source validation, correction/idempotency, restart-safe migration, HTML escaping, cached health reads and visibility after 210 historical schedules.
- Local visual review uses actual rendered routes with fictional records only. Desktop and 390 px phone layouts were checked; tested phone pages had no horizontal overflow. The loopback preview is separate from the application checkout.
- No production API calls, records, credentials, permission changes or deployment were performed by this implementation.

## Official design references

- https://developers.openai.com/api/docs/guides/live-delegation
- https://developers.openai.com/api/docs/guides/agents-api/sessions/automations
- https://developers.openai.com/api/docs/guides/agents-api/sessions/events

## Release boundary

Code and offline validation only. Do not push this branch to auto-deploying main without the parent's explicit deployment authorization. Following deployment, verify the release identity, existing owner login, the real schedule listing, cached health result and one separately authorized voice smoke test. Do not create appointment calls as a smoke test.

Independent review found no high-priority blockers. Its two nonblocking findings were corrected: the voice projection now excludes the current conversation when retrieving previous interaction context, and cached diagnostics visibly identify their recorded release, including an earlier-release notice. Targeted follow-up and the full 204-test regression suite passed.
