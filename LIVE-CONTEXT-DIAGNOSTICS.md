# Native call evidence and Live append diagnostics

A finalized first scheduled-call transcript contains a voicemail reminder followed
by the voicemail system's message-sent acknowledgment. Attendance remains
unconfirmed. A context_injection_incomplete error at teardown does not establish
that the earlier message failed.

The old error handler retained only the code, dropping the affected client event,
message, type and parameter. The exact historical append cannot be reconstructed.
Its close-time timing after a final delegation is consistent with a pending
context append, rather than evidence of failed carrier delivery. This is a bounded
inference, not a fabricated attribution.

OpenAI documents that context appends are injected over the session timeline;
closing can report errors for pending appends. An append acknowledgment estimates
context delivery and does not prove speech or playback:
https://developers.openai.com/api/docs/guides/live-conversations#understand-when-context-reaches-the-model

## Correction

- Track each greeting, optional relationship update, saved-work acknowledgment and
  spoken backend update by its unique client event ID. Handle the official append
  acknowledgment events and preserve full error attribution.
- Persist command metadata and estimated timeline positions, never appended text.
  Diagnostic writes use application-owned background work and do not block audio.
- Stop new appends and optional CRM reads once carrier input ends, including the
  interval before session.closed. Never wait for an acknowledgment, resend a
  context update, restart a session, or redial to resolve a close-time error.
- Preserve original errors, transcript, carrier and work receipts independently.
  An incomplete context update does not automatically label communication success
  or failure, and hangup does not cancel saved delegated work.
- Owner-only get_call_result now accepts existing request, call, provider, source
  or conversation identities, including native scheduled calls. It previously
  searched only dot-call request IDs. Ambiguity fails closed and assistant scope
  remains enforced. Lookup never creates/retries work.
- The authenticated dashboard links to a read-only call evidence page with raw
  and readable transcripts, transport, mission and context diagnostics. Incoming
  audio is not labeled a verified human response.

Validation uses real isolated Postgres, authenticated MCP/HTTP and actual installed
SDK event models with fake sessions. Tests cover no redial, original receipt
preservation, owner boundaries, append/ack/error correlation, teardown races,
HTML escaping and blocked diagnostic storage while audio continues. No live
latency probe, paid diagnostic, new call or external message is used for testing.

Final validation: **240 tests passed** in the full isolated regression suite.
Independent read-only review found no deployment or owner/privacy blocker. The
real first-call transcript was inspected through the repaired owner lookup before
voice-path changes; the original unattributed error remains intact.
