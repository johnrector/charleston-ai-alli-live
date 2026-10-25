# Cloud account connections

Owner setup: `/dashboard/connections`. The owner uses the existing 12-hour
dashboard session. Credentials must never be pasted into chat, model instructions,
source control, or the activity ledger.

## Google

The existing Google OAuth registration and `/google/callback` are retained.
The offline grant includes Calendar availability/owned events, Gmail sending,
and Gmail read-only. Search, thread reading and sending use account tools on
the authenticated executive MCP server. A friend's task cannot access the
private mailbox or send as the owner.

Consent uses PKCE and single-use state tied to the initiating owner session.
The identity, nonce, scopes and actual mailbox are verified before the encrypted
refresh token is replaced. The connection page permits form redirects only to
its own origin and `https://accounts.google.com`; other dashboard forms retain
their own-origin restriction. Changing this CSP can silently break consent.

Google's unverified-app warning is separate from a successful account connection.
Do not claim the app is verified because token exchange or a mailbox call passes.

## Shopify

Use Shopify's [own-organization client credentials grant](https://shopify.dev/docs/apps/build/authentication-authorization/client-credentials-grant).
The app and store must belong to the same Shopify organization. The owner creates
and installs a store app once, then enters its Client ID and secret through the
private form. Read scopes: `read_orders`, `read_products`, `read_inventory`,
`read_locations`. Credentials and cached tokens are encrypted in
`ea_account_secret`, separately from safe connection metadata. Renewal occurs
before the provider's 24-hour expiry and is serialized with a database row lock.
No Mac or ChatGPT connector session is involved.

An authorized operator can also complete the same verified import through the
existing hosting administration, without copying secrets into a conversation.
`ALLI_SHOPIFY_APP_SETUP` accepts a private JSON object with `shop`, `client_id`
and `client_secret`. Startup binds the import to the actual approved owner,
verifies the store and order access, and uses the same encrypted account store.
A matching prior import is a no-op. Malformed or rejected configuration is
audited without logging credentials and does not disable other assistant work.
Clear this temporary environment value after verifying the import. Do not
expose an import endpoint or give a model hosting-administration permissions.

The adapter only handles credentials and account I/O. The OpenAI managed agent,
GPT-6.1 Sol/high, chooses and reasons about queries using `shopify_schema` and
`shopify_query` through the existing executive MCP server. Admin API version is
2026-10. The GraphQL AST must contain exactly one read operation; mutations,
subscriptions and schema definitions are rejected before credentials are loaded.
The destination is a validated `myshopify.com` hostname. Store identity and a real
order read are checked before replacing an existing connection. Partial GraphQL
errors and pagination remain visible; protected-customer-data errors must not be
reported as successful address retrieval.

The [Shopify merchant MCP](https://setup.shopify.com/llms.txt) currently restricts
OAuth clients to supported integrations. Its metadata advertises client metadata
documents, but that does not register an arbitrary new client. Alli's attempted
client returned `unauthorized_client`. Do not spoof an approved client or copy
private ChatGPT connector credentials to bypass that restriction.

Raw mailbox and store lookup results reach the authorized model but are omitted
from the application's action receipts. Requested summaries and work results may
still be retained, and OpenAI has its own session retention. Provider access is
revocable in the owner's Google/Shopify account.

## Verification

Run the isolated Postgres test suite with `.venv/bin/python -m pytest -q`.
Connection implementation, a saved grant, and a live provider test are distinct
states. A Shopify connection is not finished until actual store calls pass.
Record production evidence in John's private Alli operations folder, not this
public repository. Deployment and logs use Render MCP and Git, without Computer Use.
