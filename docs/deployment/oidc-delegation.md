# Bounded offline identity delegation

Set `OMNIGENT_OIDC_DELEGATION_TTL_HOURS=168` and include `offline_access` in
`OMNIGENT_OIDC_SCOPES` to enable a seven-day interactive login. The default is
zero (disabled). This requires a confidential generic OIDC provider with RS256
JWKS, offline refresh tokens, and encrypted credential storage. Configure the
identity provider's client offline-session maximum and idle timeout to match.
The maximum configurable application lifetime is 720 hours.

Omnigent stores one encrypted offline refresh token per user/workspace. MCP and
Lore share that login grant and obtain separate, audience-validated access
tokens. Refresh-token rotation is serialized through the credential store;
downstream exchanges request access tokens only. Machine identities retain their
existing exchange and binding policies. No shared machine fallback is used.

Browser login issues an independent Secure/HttpOnly renewal cookie. Local access
tokens expire within fifteen minutes and HTTP requests and browser WebSocket
handshakes renew automatically from the bounded
server-side grant. Native and CLI logins use the existing refresh-grant protocol, with each local
refresh grant bound to its originating OIDC generation;
set `OMNIGENT_GRANT_MAX_LIFETIME_DAYS=7` to bound those local grants as well.
Identity-provider group claims are refreshed with the access token. Silent
renewal preserves the original authentication time and cannot satisfy a device
consent flow's fresh-login requirement. A renewal
never extends the original offline-delegation deadline.

New logins supersede the user's previous offline grant. Logout removes the stored
grant, making tokens bound to that grant fail validation. Identity-provider
revocation is detected when refreshing access. Existing short-lived tokens can
remain usable until that refresh/expiry boundary. Once the grant expires, the
user must sign in again; MCP authentication rejections return an actionable error
rather than waiting for transport timeouts.

Enable offline access at the identity provider before enabling the application
setting. Existing local login tokens retain their previous lifetime but do not
create offline delegation. Local refresh grants without an OIDC generation
cannot renew after activation; users must complete a new interactive sign-in.
The nullable generation column is added by the application database migration. This
feature does not change the provider's realm-wide SSO session policy. To disable
it, set the lifetime to zero and remove `offline_access` from the requested
login scopes. Grant-bound tokens then require a fresh legacy login.

Verify browser, native and CLI login and renewal, connectors and Lore access,
concurrent refresh, group removal, logout/revocation, and absolute expiry. Keep
credential values out of logs and test evidence.
