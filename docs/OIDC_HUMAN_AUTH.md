# OIDC delegated human authentication

An OIDC server can accept access tokens from explicitly allowlisted human OAuth
clients at its session API. These are JWTs from the same issuer as machine
access tokens and browser login. Signature/issuer/audience/lifetime validation
is shared; machine and human identity resolution are separate, with no fallback.

```yaml
oidc_human_auth:
  audience: agent-api
  scope: agent-access
  clients: [tool-exchange]
```

This is opt-in and requires OIDC mode, HTTPS issuer/JWKS URLs and a permission
store. Human and machine client allowlists must not overlap. Allow only clients
whose user/delegation flows preserve the original human subject, with machine
login disabled. The required scope must be present as a complete whitespace-
separated value in the access token; a similar prefix does not match.

Send `Authorization: Bearer <access_token>`. RS256, `kid`, exact issuer, API
audience, `typ: Bearer`, nonempty subject, allowed `azp`, valid integer `iat/exp`
and a lifetime at most one hour are required. Tokens in cookies are not accepted.
Client-credentials markers (`client_id`, `clientId`, `is_service_account`),
service-account usernames and configured machine subjects are rejected on the
human branch. A nonempty `preferred_username` is required. The issuer must
preserve these identity claims across delegation; do not configure clients that
can mint arbitrary human subjects or strip machine provenance.

The email uses the same configured claim and verification policy as browser
login, including explicit operator verification overrides. It is trimmed and
lowercased. Missing email, reserved identities and machine namespace identities
are rejected. The existing domain/admin/invite admission policy is consulted on
each request, without an identity cache. Accepted users are registered/promoted
through the same local admin roster as browser login; JWT roles never promote
users. Session ownership and grants use that same canonical identity.

Delegated tokens are confined to agents/hosts/sessions/skills/runners and the
existing delegated OAuth paths. `/v1/me` and account/admin endpoints remain
outside this path allowlist. Inspect `GET /v1/sessions/{id}/owner` to verify
canonical ownership. Sending `created_by` or a user identity header is not a
supported way to impersonate a person.

For a token-exchange broker, obtain a token with the API audience, required scope,
original user's subject and verified email, then forward it. Keep tokens isolated
per caller/audience and renew before expiry. User login alone does not give a
broker arbitrary impersonation authority. Removing the human-client binding
rejects new requests after config reload; issuer-side role/client revocation
otherwise takes effect as issued JWTs expire. Running agents use independent
Omnigent runner tokens. Do not automatically retry non-idempotent writes.

Verify with a disposable session: delegated creation and browser login must
resolve to the same owner, a second user must be denied until explicitly shared,
edit grants must not allow deletion, revocation must deny later access, and the
owner must be able to delete. Reject a wrong audience/client, a service-account
subject, missing scope and unverified email. No token or credential belongs in
logs, prompts, session URLs or tool results.
