# Scheduled executive work

Approved contacts can create bounded one-time schedules for themselves. Each
occurrence retains the original friend scope and current contact status. The
normal result outbox reports to that person's saved number. It does not grant
access to John's accounts or permission to contact others. Recurring schedules
remain owner-only. Outstanding and daily limits use the contact work budgets;
suspension revokes pending launches and queues provider cleanup. Reactivation
does not restore cancelled schedules. John can inspect and manage every schedule.
Accepted schedules reserve their future occurrence. Activation uses the normal
work queue and its execution capacity; a busy contact does not lose an accepted
reminder. Standing friend work cannot create or mutate further schedules.

Scheduling means future calls, texts, emails or other authorized work, not a
calendar placeholder. Calendar bookings remain a separate tool.

OpenAI's native Agents Automations API owns the clock, one-time Unix timestamps
and recurring cron expressions with IANA time zones. No Render cron service,
sleeping process, Twilio scheduler or second application reasoning loop is used.
The installed Python SDK predates the automation wrapper, so a small adapter
uses the documented REST endpoints through the existing authenticated client.

An authorized schedule has an application ID, revision, owner, original request
and native automation ID. The native target starts a fresh managed session for
each invocation. That session can call only `dispatch_scheduled_work`; its
credential cannot access accounts, make calls or send messages. The adapter
verifies the actual native invocation and its provider-retained instruction
snapshot, then creates one ordinary authorized assignment keyed by invocation
ID. The normal managed Agent performs the work with a fresh, short-lived tool
grant. This avoids keeping an expired phone-session grant alive for next month
and gives each occurrence independent action/call idempotency.
The I/O observer recovers missed launch callbacks from the same recorded native
occurrence and invokes the same dispatcher. It never computes when work is due.

Both agents use GPT-6.1 Sol/high. GPT-Live retains full-duplex conversation and
delegates to the backend. Twilio remains transport only. Cloud work does not use
the Mac; explicitly local work retains its normal executor requirements.

Saved, triggered, working and completed are distinct. Invocations and their
actual work receipts share the existing activity ledger, owner portal and Dot
event channel. A native invocation marked completed means its session accepted
the input, not that a call was answered or an email/text delivered. Provider
failure is recorded and reported; uncertain writes are never silently retried.

Owners can list, update, pause, resume and cancel schedules. Revisions serialize
changes. Provider-retained snapshots preserve already queued invocations when
the schedule changes. Pause/cancel prevents future starts; it does not retract
an accepted communication. Existing security, contact suspension and quotas
still apply at execution. Friends cannot schedule owner account operations.

Existing already-authorized SMS commitments remain intact. New future SMS
requests use this native scheduler; immediate SMS delivery remains in the normal
durable transport outbox. No birthday message or other commitment is duplicated.

Sources:
- https://developers.openai.com/api/docs/guides/agents-api/sessions/automations
- https://developers.openai.com/api/docs/guides/live-delegation
- https://learn.chatgpt.com/docs/dots/tasks-and-memory
