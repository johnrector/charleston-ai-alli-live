# An executive assistant, reached by phone, text and Dot

Alli exists to act as John Rector's AI executive assistant. Her identity,
relationships, discretion, judgment, commitments and follow-through define the
system. Transport, storage and the private oversight dashboard support that role.

## Ownership

- OpenAI GPT-Live owns listening, speech, interruptions and conversational timing.
- OpenAI GPT-6.1 Sol, with high reasoning, owns reasoning and tool selection.
- The application derives permissions, executes authorized tools, records facts
  and receipts, and retains unfinished commitments after a conversation ends.
- Twilio transports phone audio and messages. It does not classify answers,
  run a conversational workflow, decide intent, or hang up on voicemail detection.

Use OpenAI's native SDK directly. OpenAI's managed Agents API owns the reasoning
loop and durable sessions. No Twilio Agent Connect runtime, custom step-count
loop, article engine or visual-review workflow.

## Text conversation and delegated work

Identify the signed SMS sender by normalized phone number before asking the
conversation model to speak. Supply the saved name and actual permissions;
Blake Miller saying hi is Blake, never a request to authenticate as John.
Recognition personalizes conversation and cannot grant owner authority.

Use a compact Responses conversation with GPT-6.1 Sol/high. It answers chat or
calls the single work-delegation function. It has no account tools. Save the
original authorized request as a normal managed Agent assignment before sending
the model's acknowledgment. Do not rewrite its authority from generated text.
Managed work continues after the text interaction ends, with the same tools,
receipts and Dot updates as phone work. A greeting starts no work Agent.

This applies OpenAI's documented manager/specialist pattern to SMS; the Live
guides describe a spoken frontend, not a separate documented SMS transport.
Inference and transport failures are separate: resume a saved result without
generating or delegating it twice, and retain original work if text inference
fails. Serialize turns per person while independent conversations and work
continue. Explicit silence applies only to the current assignment. The common
outbox verifies actual carrier receipts; an acknowledgment is never work done.

See [OpenAI agent orchestration](https://developers.openai.com/api/docs/guides/agents/orchestration#use-agents-as-tools-for-manager-style-workflows)
and [latency guidance](https://developers.openai.com/api/docs/guides/latency-optimization).
See [SCHEDULING.md](SCHEDULING.md) for native cloud scheduling and its distinct
saved, invoked and completed states.

## Small, explicit boundaries

1. **Identity and charter:** a configured assistant and principal, speaking style,
   public facts, standing permissions and operating limits.
2. **Relationships and memory:** recognized phone numbers, approved contacts,
   relevant prior conversation and stated preferences. Recognition personalizes
   conversation; private owner access still needs possession proof.
3. **Commitments and work:** durable assignments, scheduled messages and verified
   provider outcomes. Hanging up releases the call, not the assignment.
4. **Tools:** named, typed operations bound to the authenticated requester.
   The model cannot supply its own permissions or change the sender identity.
5. **Channels:** phone, SMS and owner-authenticated MCP/Dot translate into the
   same domain services. Friends need only the saved phone contact.
6. **Storage:** repository interfaces isolate the core from SQL and table names.
   Keep the existing Render Postgres database and useful records. No wholesale
   data migration. Add storage structure only where a verified requirement needs it.
7. **Oversight:** append-only activity and provider receipts in John's dashboard.

## Voice connection

Use the documented server audio bridge with the native OpenAI Live connection:
continuous G.711 mu-law audio, 8 kHz, with no application VAD or turn-taking model.
The existing project key and phone number support this path. Direct SIP remains
an alternative, not a prerequisite: OpenAI documents project/organization
enablement and provider setup requirements for it.

Configure Live client delegation. Keep its prompt short and use OpenAI's documented
SpawnThinking instructions. Persist the transcript and assignment when Live
requests delegation. Hand work to a managed Agent using GPT-6.1 Sol and high
reasoning. Its service-origin MCP connections execute authorized tools. Return
verified results to Live with the original delegation ID while the call remains
open. OpenAI continues work after the call or submitting SDK client disconnects.

## Execution and existing plugins

Use no computer environment for ordinary remote-tool work. Use an OpenAI-hosted
environment when the assignment requires cloud files or computation. For John's
assignments requiring local files, software or desktop plugins, his Mac mini is
an authorized execution environment. OpenAI's official `codex exec-server`
executes commands and local MCP tools there; the managed OpenAI Agent remains
in charge of reasoning. This is an executor, not a second local assistant.

Mac work waits durably when the mini is offline. Phone, text and remote tools
remain independent. Each managed session has its own environment and executor;
reconnection reuses that environment rather than replaying its assignment.
Use a separate environment-only API key; never pass the application API key into
the executor. Connections are outbound; do not expose a desktop-control port.

Load compatible local plugin roots through the documented capability directories.
Do not bulk-copy private skill snapshots into Render. Plugin availability and
account authorization are separate: validate the actual tools and credentials.
The managed API does not assume it inherits ChatGPT's connected-account tokens.
John's authorization to use the Mac does not grant friends access to his files.

## Dot

Expose authenticated MCP tools and MCP Events using protocol 2026-07-28. Dot is a
documented Events client and can subscribe to assignment and call updates. The
same durable records drive John's portal and Dot. Do not pretend that a native
Dot and an API session automatically share private memory or account credentials.
Subscriptions and their callbacks belong to the authenticated owner. A callback
acknowledgment confirms delivery, not completion of work in Dot.

## Persistence and recovery

Save task state and action claims before external writes. Save actual receipts
afterward. Reuse a completed action's receipt after restart. A write interrupted
without a receipt is uncertain and requires review; it must not be retried under
a new identity. A new read sees current state.

Live delegation and after-hangup monitoring share the same assignment and action
ledger. Ending a call never cancels a managed Agent. Native session creation is
not assumed idempotent: save dispatch intent, persist the session ID, and recover
an uncertain creation by matching task metadata rather than creating another.
Follow-up input uses the API's documented idempotency key. Idle session status
alone is not success; reconcile the turn outcome and actual provider receipts.

## Release bar

Real incoming and outgoing human phone conversations; completed work after hangup;
real SMS through email/Calendar/delayed delivery; native Dot-to-portal receipts;
persistent owner sign-in; suspended/unknown contact boundaries; restart and
uncertain-write tests. Configuration and unit tests alone are not a release pass.

## OpenAI sources checked October 24, 2026

- https://developers.openai.com/api/docs/guides/voice-websockets?api=live
- https://developers.openai.com/api/docs/guides/live-delegation
- https://developers.openai.com/api/docs/guides/live-migration#review-both-prompts
- https://developers.openai.com/api/docs/guides/voice-sip?api=live
- https://developers.openai.com/api/docs/guides/agents-api/architecture
- https://developers.openai.com/api/docs/guides/agents-api/sessions
- https://developers.openai.com/api/docs/guides/agents-api/environments/self-hosted
- https://developers.openai.com/api/docs/guides/agents-api/environments/openai-hosted
- https://developers.openai.com/api/docs/guides/agents-api/tools/plugins
- https://developers.openai.com/plugins/build/mcp-events
- https://learn.chatgpt.com/docs/dots/computers-and-apps
