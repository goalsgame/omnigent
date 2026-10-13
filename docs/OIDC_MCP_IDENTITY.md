# Owner-bound outbound MCP identity

Managed hosts can use an operator-configured MCP upstream with their owner's
OIDC identity. Browser, native and CLI login share the callback that captures
the verified owner's grant. API session requests carrying an independently
verified IdP human or machine bearer capture the same grant. Locally signed
Omnigent session tokens cannot create a delegation.

The runner connects through a stdio bridge using its existing managed-host
launch credential. The server resolves the host's stored owner, exchanges or
refreshes that owner's encrypted grant, and forwards the MCP request. IdP access
and refresh tokens stay on the server. A missing, revoked or expired grant
fails closed; there is no shared service-account fallback. Shared-session readers
and participants do not replace the managed host owner's identity.

## Server configuration

This opt-in feature requires OIDC login with a confidential client, the existing
credential cipher, a database-backed credential store and managed hosts. The
machine exchange client uses `client_secret_post`.

```yaml
oidc_mcp:
  upstream_url: https://tools.example.com/mcp
  audience: tools-api
  scope: openid tools-api
  machine_client_id: tools-machine-exchange
  machine_client_secret_env: MCP_EXCHANGE_CLIENT_SECRET
  # Optional distinct requested scope for machine grants.
  machine_scope: openid tools-api application-api
```

Mount the client secret into the named environment variable. The fixed HTTPS
upstream cannot be changed by a runner request. The relay accepts MCP POST, GET
and DELETE, streams responses, binds upstream session IDs to the host and owner,
and strips caller Authorization, cookies and host credentials before forwarding.
It does not retry upstream operations after authentication or transport failures.

Configure each managed agent's tool:

```yaml
tools:
  tools:
    type: mcp
    transport: stdio
    command: python3
    args: [-m, omnigent.host.mcp_identity]
```

Both server and runner must contain the implementation. The bridge requires
managed-host credential coordinates; an arbitrary local host does not inherit
the server-side identity. Each relay request also requires an online host with a
fresh heartbeat. Suspended, disconnected, and stale hosts cannot invoke tools;
access resumes after an authenticated host reconnect.

## Identity provider and upstream policy

Enable standard token exchange with same-session refresh tokens for the login
client and the dedicated machine exchange client. Each incoming subject token
must audience the requester. Machine client-credentials tokens must come from
persistent service-account sessions; transient sessions cannot supply a
same-session refresh grant. Do not request offline access.

The exchanged access token must retain the original subject, use the expected
requester as `azp`, and include the configured upstream audience. The exchange
request uses scopes without an audience filter so role-bearing scopes are not
removed. Configure audience mappers on those scopes for each required service
and onward exchange client. The configured `audience` is required when validating
the returned access token. Configure
upstream authorization for the human requester's verified groups and each
machine requester's exact original subject. Sharing a requester does not imply
sharing grants. Keep human and machine requester client IDs distinct.

If the upstream calls back into Omnigent as a machine, configure
`oidc_machine_auth.exchange_clients` with the trusted requester IDs. Exchanged
tokens must still match exactly one original machine subject binding, carry the
required audience and automation role, and pass the current non-admin binding
check. Direct machine token verification remains unchanged.

## Renewal and rollout

Grants are encrypted per owner. Refresh uses PostgreSQL row locks or SQLite per-credential file locks with
compare-and-swap, so replicas do not rotate the same refresh token concurrently.
SQLite provider requests run outside database transactions; unrelated writes
remain available while refresh is pending. Removing a machine binding or
promoting its principal to admin prevents further use. Provider errors require
a fresh sign-in or API delegation; they never select another identity.

Enable IdP exchange and upstream grants before enabling this configuration.
Existing browser sessions need a fresh sign-in; their local cookies do not hold
an IdP token. Existing machine callers should obtain a fresh IdP token and send
a session API request. Sleeping hosts reuse the owner's stored grant on wake.
Agent tool definitions saved in older sessions may need an explicit migration
or a replacement session; changing the catalog does not rewrite saved bundles.

Renewal ends with the originating IdP session. An already issued access token
can remain valid until its short expiry. Omnigent's local logout clears its
session cookie; terminating the IdP session controls provider-side revocation.
