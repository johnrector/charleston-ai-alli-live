# Finite voicemail playback and hangup

The original call left its reminder, then continued listening to the mailbox menu. Closing a call depended on a later delegated Agent action. Primary GPT-Live streams do not expose a reliable output-audio-done boundary, so a transcript sentence, timestamp, silence gap or context acknowledgment cannot authorize hangup after actual playback.

The outbound voicemail path now has one finite message prepared before dialing, using the existing OpenAI speech API and configured Marin voice. Appointment text contains only the saved recipient name and bound calendar time/place; other missions use a generic callback message. Existing call-specific `hang_up` preference skips rendering and leaves no message. Failed or uncertain rendering holds the never-dialed call without a retry. Appointment holds appear in existing diagnostic coverage.

Twilio `DetectMessageEnd` runs asynchronously, leaving the human GPT-Live path connected immediately. A signed callback must match the saved call and provider identity. Machine acoustic readiness is corroborated against untrusted transcript evidence by the existing Responses work model; human, unknown, fax, ordinary IVR and ambiguous evidence do not authorize a message. No Decisions permission or new credential is requested.

Live listens for an actual human answer before the outbound mission greeting and remains silent over recorded greetings. The application sends the finite PCMU asset and a unique final Twilio mark. Only that uncleared mark's acknowledgment authorizes one carrier completion request. Clear, disconnect, stale/unknown marks and transport deadline do not count as played. The media-sized deadline is an acknowledgment failure bound; it never ends a silent call. Incoming audio/speech or human detection cancels playback/hangup, invalidates marks, and restores ordinary Live output. Playback and delegated-end reservations prevent races; actual end actions retain durable once-only receipts. An uncertain carrier result is never retried.

No research, rendering, classification or provider acknowledgment is awaited in normal Live startup/audio handling. Optional voicemail initialization runs after `session.started`. Source freshness and existing appointment authorization checks run after speech preparation and immediately before dialing. Normal inbound calls retain their existing path. No separate scheduler, campaign, CRM database or new infrastructure is added.

## Evidence and limits

`ea_call_voicemail` stores one asset/attempt per existing call, source text/digest, detector state, playback state, hangup state and timestamps. Raw audio is owner-side storage, omitted from tools/dashboard and model context. Owner call lookup, diagnostic call page and mission context expose separate finite-message receipts. Generated Live transcript captured while application output is suppressed is labeled unverified, so it is not silently promoted to spoken dialogue. A carrier mark proves playback, not mailbox storage or attendance confirmation.

If any Live audio has already been sent, the finite reminder is held to avoid duplicate or truncated speech; the ordinary delegated end remains available. Ambiguous detection or missing playback acknowledgment also holds automatic voicemail control. These are visible exception states, not success. Runtime TTS capability, AMD accuracy/noise behavior and real-world timing are not established by mocked tests. Production provider refusal does not grant scopes or trigger a fallback/retry. Standard per-use speech/classification/AMD charges may apply to future authorized calls; no subscription, service provisioning or probe is part of this change.

The migration is additive and idempotent: one table, a composite unique index for its scoped foreign key, and additive columns. Old calls are not replayed, re-rendered or backfilled. Old code can run with these unused additions left in place. Retain rows/receipts on rollback; do not drop historical evidence.

Verification uses isolated disposable Postgres and mocked provider APIs, covering finite byte ordering, clear/stale marks, interruption during lock/DB windows, classification races, human recovery, signed callback identities, no-message preference, missing acknowledgment, rendering/classification failure, uncertain teardown/hangup, and once-only actual end requests. No live phone/SMS or paid provider probe is authorized for this repair.

## Official protocol sources

- [OpenAI Live conversations](https://developers.openai.com/api/docs/guides/live-conversations): primary audio/transcript streams and client delegation.
- [OpenAI playback controls](https://developers.openai.com/api/docs/guides/voice-server-controls#control-playback-when-needed): application-owned playback and finite assets.
- [Twilio Media Streams WebSocket messages](https://www.twilio.com/docs/voice/media-streams/websocket-messages): outbound media, mark acknowledgments and clear returning discarded marks.
- [Twilio answering machine detection](https://www.twilio.com/docs/voice/answering-machine-detection): asynchronous DetectMessageEnd and callback values.
- [OpenAI text to speech](https://developers.openai.com/api/docs/guides/text-to-speech): finite speech generation and raw PCM output.

## Exact owner-authorized message and silent completion

For an explicit exact voicemail, the existing owner `call_outbound.approved_logistics` string accepts a JSON object containing only `voicemail_message` (1–800 characters, no control characters) and optional `completion_notification` (boolean). Message text is persisted separately and read verbatim, never inferred from mission prose. Plain logistics text retains its prior meaning. `completion_notification:false` places a server-enforced `notification_policy:none` on the owner call task and its new preparation/voice-child tasks; model output cannot re-enable completion SMS. Existing stable request identity includes the original logistics object; it cannot be used to change/replay a call. This does not grant calling, messaging or account authority.
