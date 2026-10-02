# Charleston AI — Alli Calls

## Cloud plugin description

Make mission-driven outbound phone calls with Alli on John's behalf: confirm or reschedule appointments, ask questions, coordinate work, and follow up. No business card or fixed script required. GPT-Live handles the conversation with its own delegated reasoning and calendar/email tools. Calls run while the dot continues other tasks; retrieve saved results afterward. Use other connected tools for Shopify, Maps, invoices, and standalone email.

## Dot workflow

The dot coordinates the whole request; this plugin supplies the phone capability.
Use the connected cloud tools directly, without a Mac/browser relay.
During each call, GPT-Live retains its own delegated reasoning and calendar/email
tools. The dot need not handle those in-call actions or wait for the call to end.
This update changes no voice model, reasoning model, TAC version, audio settings,
delegation mechanism, credentials, or tool implementations.

1. Split the request into concrete outcomes. Start independent authorized work
   without waiting for calls to finish. Resolve necessary dependencies first.
2. Use current connected sources for recipients, phone numbers, appointment
   times, order addresses, and billing details. Never invent a missing contact,
   rate, location, or result. Ask only for essential missing information and
   continue unrelated work.
3. For each authorized phone conversation, prefer `call_outbound`. Supply its
   verified recipient, a clear mission, only relevant shareable context, the
   capabilities needed for the task, and a stable unique request ID. No card,
   image, or preset is needed. Optional presets guide the opening only.
4. For confirmation with possible rescheduling, provide the appointment time
   and timezone, and grant check_calendar/manage_calendar. Grant create_meeting
   only if new bookings are authorized, and send_email only for authorized
   related email. Explicit [] means conversation only. Existing owner-enabled
   defaults remain available when capabilities are omitted.
5. Preserve returned request IDs. Accepted/queued means dialing started, not
   that someone answered. Read progress and outcomes with `get_call_result`;
   never redial an uncertain attempt using a new ID. A busy-recipient response
   points to the existing intent and is not a reason to bypass deduplication.
6. Use Shopify/Maps, billing, and email tools directly for non-phone tasks.
   An invoice email does not require a call. Route links are not proof a route
   was saved to Maps; an email request is not proof of delivery; a call report
   is not independent verification of a calendar change.
7. Briefly acknowledge the overall work and report each outcome separately
   once verified. Do not force every response to be only "Calling."

These instructions grant no new permissions and start no work by themselves.
Illustrative demos are not live authorizations. Automatic future calls remain
off unless separately configured and authorized.

## Example task mapping (do not execute)

| Spoken work | Dot coordinates | Alli phone mission |
| --- | --- | --- |
| Put unfulfilled Shopify orders on a Maps route | Read orders, resolve addresses and route start/end, create the supported route artifact | None unless a call is explicitly requested |
| Confirm afternoon appointments; reschedule if needed | Read the actual calendar, resolve contacts, start each authorized call, inspect results | Confirm the identified meeting and agree a new time if needed; update the existing event |
| Email Blake an invoice for three hours onsite | Resolve Blake, the agreed rate and billing details; create invoice and send through the appropriate tools | None |

## Refresh after deployment

Update the private cloud plugin's description with the text above, refresh its
MCP tools, and verify call_outbound/get_call_result in the dot's cloud context.
Use a saved call result for the connectivity test. Real call behavior requires
a separately authorized recipient and mission; local tests do not establish it.
