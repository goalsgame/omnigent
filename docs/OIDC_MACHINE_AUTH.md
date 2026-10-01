# OIDC machine authentication

An OIDC deployment can accept service-account access tokens directly from its
login issuer. This is separate from Omnigent's own `/oauth/token`
client-credentials grant. The external issuer owns client credentials and issues
access tokens; Omnigent stores only public authorization configuration.

Add this section to the server config file:

```yaml
oidc_machine_auth:
  audience: agent-api
  role_client: web
  role: automation
  clients:
    ticket-worker: service-account-subject
```

The issuer and JWKS endpoint come from the existing OIDC login configuration.
Both URLs must use HTTPS. This feature is available only in `oidc` mode, remains
disabled when the section is absent, and rejects malformed configuration at
startup. A permission store is required.

For Keycloak, create a confidential client with service accounts enabled and
browser/password flows disabled. Give it the API audience and a role scope
mapping for the required client role, grant that role to its service account,
and include `sub` in access tokens. Configure the client's exact service-account
user ID in `clients`; neither a username nor an email is a subject binding.
Issue short-lived tokens using `grant_type=client_credentials` and send them to
Omnigent as `Authorization: Bearer <access_token>`.

Tokens must use RS256 with a signing-key ID. Omnigent verifies the signature,
issuer, audience, expiry and issue time, requires `typ: Bearer`, and matches
`azp` and `sub` against the configured binding. The required role must appear in
`resource_access.<role_client>.roles`. Tokens with a lifetime over one hour are
rejected. An email claim never selects the machine's identity.

The machine owns sessions as `oidc-machine:<client_id>`. This namespace is
reserved from human OIDC login. Machine principals cannot be administrators:
startup checks the admin roster and permission store, and each authenticated
request checks again. Removing a binding rejects subsequent authentication,
including Omnigent-issued owner tokens used by existing machine runners.

External machine tokens use the existing delegated API path allowlist for
agents, hosts, sessions, skills and runners. They cannot access human login or
admin/account-management endpoints. Session ownership and sharing permissions
still apply. To let a person continue the work, grant them edit access through
`PUT /v1/sessions/{id}/permissions`; posting a session URL alone does not grant
access. Public link sharing is read-only.

JWKS sets are cached for five minutes. Unknown signing-key IDs trigger a refresh,
with network refreshes limited to once per thirty seconds per server process.
A recently rotated key can therefore require a retry after thirty seconds.
Issuer requests have a five-second timeout and run outside the event-loop
thread. Failed verification or key retrieval rejects authentication.

Disabling a service account or removing its role at the issuer takes effect for
API access when existing access tokens expire. Omnigent does not introspect each
token. Already-running agents use separate Omnigent-issued runner tokens and
can continue working; stopping their sessions is a separate lifecycle action.

Verify a deployment with a real service-account token: list agents, create a
managed session using a registered agent, inspect its owner and grant a human
edit access. Confirm a token for another audience or subject is rejected, and
that the machine cannot read an unrelated person's private session. Human
browser and CLI login should continue to work with their existing credentials.
