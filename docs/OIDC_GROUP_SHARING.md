# OIDC group session sharing

OIDC deployments can share a session with an exact group name from the
issuer's signed `groups` array. The claim must be present in login ID tokens
and, for delegated human API access, access tokens. Group emails are not
looked up in a directory. `engineering` and `/engineering` are distinct names;
use the value emitted by the issuer.

In the session's Share dialog, choose **OIDC group**, enter its exact name and
choose Read or Edit. The dialog identifies existing group grants separately
from individual users. All clients using this dialog share the same behavior.
Group support is advertised as `group_sharing_enabled` by `GET /v1/info`.
Header, accounts, and GitHub login deployments do not enable it.
Only an active OIDC provider restores group authority. GitHub OAuth ignores
stored OIDC membership claims, including in existing login and refresh tokens
when the signing secret is retained across an authentication-provider switch.

The permissions API also supports Manage:

```http
PUT /v1/sessions/{session_id}/permissions
Content-Type: application/json

{"principal_type":"group","user_id":"/engineering","level":1}
```

Levels are Read (1), Edit (2), and Manage (3). The response includes
`principal_type: "group"`, `group_name`, and an opaque `user_id` beginning with
`oidc-group:`. Use that returned `user_id` in the existing DELETE permission
endpoint to revoke the grant. Existing individual-user requests omit
`principal_type` and continue to work. Names must contain 1–87 UTF-8 bytes,
with no surrounding whitespace or control characters.

Members inherit the highest of their individual and group grants. Shared
sessions appear in session listings, searches, and the Shared view; they do
not become the member's owned sessions. Child sessions inherit the parent's
access as usual. Group grants cannot confer ownership or server admin status.
A read-only member can fork the conversation into their own session. Forking
copies history, not the original sandbox's filesystem or personal credentials.
The server's sharing restrictions also apply to groups.
Leaving an individual share does not remove access inherited from a group.

Membership comes only from verified authentication. Machine identities do not
inherit groups, even if their issuer token contains a `groups` claim. Browser,
CLI and native login tokens retain the verified membership until the original
login's session lifetime expires. Login and device refresh grants preserve
that same deadline; token refresh cannot extend group authority. Sign in again
to refresh membership. Delegated OIDC human tokens use their own expiry.
Directory membership changes therefore take effect on renewed authentication
or expiry; they do not revoke already issued login tokens immediately.
Membership arrays must contain at most 256 entries and fit within 1536 bytes
of compact, ASCII-escaped JSON after deduplication. Malformed or oversized
claims confer no group authority; individual permissions remain available.
Configure the issuer to emit only the groups needed for session sharing.
Revoking a session's group grant uses the normal permission-cache invalidation
and cross-replica cache lifetime.

Existing logins and refresh grants contain no group authority. After configuring
the issuer and deploying group support, members must sign in again. Startup
adds a nullable group-authority column to device grants and an explicit group
discriminator to permission grants. Existing grants are always marked individual,
even if their IDs begin with `oidc-group:`. A group share that conflicts with an
existing individual key returns HTTP 409; revoke the conflicting grant before
adding the group share. On MySQL, startup also changes permission keys to
`utf8mb4_bin` comparison so case-sensitive encoded
group keys cannot collide; existing key values are preserved. All MySQL
permission principals therefore compare case-sensitively after upgrade.
Downgrade removes explicit group grants and retains this binary comparison to
avoid merging distinct keys. Existing grants retain their individual permissions
and refresh behavior.

To verify a deployment, share a session with a known group as Read. A newly
signed-in member should see it under Shared, read it, and fork it, but should
not edit or delete the original. A non-member should not see or open it.
Upgrade to Edit and verify the member can send a message; revoke the group
grant and verify access disappears. Repeat through delegated human API access.
Keep the member's session list open while granting access; the shared session
should appear without a reload.
