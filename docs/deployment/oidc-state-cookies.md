# Concurrent OIDC sign-ins

`OMNIGENT_OIDC_FLOW_STATE_COOKIES=true` gives each pending browser, CLI or native
sign-in its own signed, HttpOnly state cookie. This prevents another tab from
overwriting its state or PKCE verifier. State signatures, expiry, state matching
and PKCE verification remain required. Pending state expires after five minutes;
new login responses retain at most eight visible pending cookies, including a
legacy cookie. Pending state cookies also share a 4 KiB byte budget; the login
response evicts older state as needed and reserves room for session/other cookies
within a 6 KiB Cookie-header budget. A single state cookie is capped at 3,800 bytes
before attributes, leaving room below browsers' 4 KiB cookie limit. Requests that
cannot fit even after evicting old state fail before redirecting to the IdP;
shorten the return URL or clear site cookies before retrying.

Simultaneous login responses cannot see each other's cookies, so
the limit is enforced against the cookies present on each incoming login request.

The setting defaults to false: servers issue the legacy single state cookie and
accept both cookie formats. For a rolling deployment:

1. Deploy servers that understand both formats with the setting unset or false.
2. Confirm all login and callback replicas run that version.
3. Enable the setting across the deployment. Replicas with either writer setting
   can consume both formats, so this configuration rollout can overlap safely.

A deployment that drains every older callback handler before exposing the new
server can enable the setting in the initial rollout. Never route a flow-cookie
callback to a server version that only understands the legacy cookie.

Before rolling back to legacy-only handlers, disable the setting on all writers
and wait at least five minutes for existing flow cookies to expire. Disabling it
preserves sign-in compatibility but restores the single pending-login behavior.
