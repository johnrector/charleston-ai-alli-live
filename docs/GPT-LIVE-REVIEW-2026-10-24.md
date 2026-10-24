# Official GPT-Live review — October 24, 2026

Primary sources read in full or by the relevant sections:
- https://developers.openai.com/api/docs/models/gpt-live-1
- https://developers.openai.com/api/docs/guides/live
- https://developers.openai.com/api/docs/guides/live-prompting
- https://developers.openai.com/api/docs/guides/live-delegation
- https://developers.openai.com/api/docs/guides/live-conversations
- https://developers.openai.com/api/docs/guides/voice-server-controls?api=live
- https://developers.openai.com/api/docs/guides/live-partner-integrations

The current model page identifies gpt-live-1 as the current snapshot. GPT-Live
owns continuous listening/speaking, interruption behavior and delegation timing.
The independently selected backend remains John's requested GPT-6.1 Sol high.
Responses delegation supports its reasoning setting and application functions.
Twilio Agent Connect is documented as a partner transport integration.

Applied guidance: compact conversation policy with the documented headings and
SpawnThinking trigger; separate detailed backend rules; verified tool outcomes;
application-owned permissions and durable task state; preserve text fragments
and their start_ms/end_ms; graceful Live session closing; one executor for a
function call. No manual input-audio commits, VAD turn triggers, Realtime-family
substitution or Twilio conversational decision engine was introduced.

Our primary WebSocket already carries events server-side, so a second sideband
is unnecessary. The narrow pinned-provider extension observes this stream;
standard TAC audio forwarding and GPT-Live session lifecycle remain in place.

A live session alone is not a durable post-hangup job store. Postgres checkpoints,
job receipts and post-call recovery implement the personal-assistant promise.
Mixed input audio has user/assistant transcript roles, not verified individual
human identities. Explicit introductions and bound recipient numbers determine
per-person distribution; no voice-biometric identity claim is made.

Public basics verified from https://ai-chs.com/ and /classes/: daily 9–5 Eastern,
405 Jessen Ln Suite E, Charleston SC 29492; education $60/hour, professional
services $160/hour, managed services $1.68/hour of AI time. These are lab rates,
not a fee for talking to Alli or proof that every meeting with John is paid.
