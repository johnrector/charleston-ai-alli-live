# Restore the working conversational voicemail path

Restore application code to d19a0e8787d8f9b0f6324100cffbbc528215f624. This keeps the existing GPT-Live conversational voicemail behavior that left the appropriate reminder, while preserving that release's CRM/people preparation, native appointment scheduler, owner call lookup and context diagnostics.

The subsequent finite-voicemail/AMD control, speech rendering dependency, transcript/audio gates and dependent test-only options are removed. The two explicitly authorized owner tests failed to play the finite message. No further test call, SMS, email, paid probe, credential or permission change is part of this rollback.

Delayed hangup remains a known issue. The user explicitly accepts the prior timeout behavior. Do not add a new hangup mechanism or redesign as part of this release.

Database rollback is additive-safe: do not drop the now-unused voicemail table, indexes, or columns; do not delete or rewrite call transcripts, media evidence, model errors, provider receipts, activity history or policy/schedule records. Existing production rows retain their evidence, even though the restored runtime does not use the rejected voicemail control path. No calls or old schedule invocations are replayed.

Verify with the restored regression suite, exact Git/Render release identity and public health. Read existing call receipts only; do not place another call to verify restoration.
