# Read-only internal session sharing

Set `OMNIGENT_INTERNAL_SHARING_GROUP` on the server to an **exact verified OIDC
group name**, for example `employees`. Startup rejects an empty/malformed group
or an authentication provider without verified OIDC group support. Email suffixes,
client-supplied headers and arbitrary authenticated users are not an internal
audience. The identity provider must actually emit that group's membership in the
Omnigent login and delegated-human token claims. Machine identities do not inherit
human group authority.

With this deployment configuration, `/v1/info` advertises the internal group and
the conversation Share dialog offers **Share internally**. Turning it on writes
an ordinary typed OIDC group grant at read level (1); turning it off revokes that
encoded group grant. The configured audience cannot be newly granted edit/manage
access through the API. Other individually granted collaborators retain their
own permissions. Read access includes conversation outputs and permits forking;
workspace files remain behind the existing separate files-sharing toggle.

The policy hides/disables new public-link sharing and cannot be relaxed by saved
admin public-sharing settings. It does **not** silently rewrite existing ACLs.
Old public-link grants remain listed with a revoke action; old higher-level grants
to the internal group show a warning and can be revoked/re-created read-only.
Before enabling a new IdP group projection, audit existing exact-name group grants:
previously unmatched grants may become effective once users get membership.
Revoke unintended public/overprivileged grants with the owner's approval. Removing
public UI alone is not a revocation mechanism.

In an authenticated deployment, the existing public-link grant still requires
login to access session read APIs; it means any identity accepted by that server
with the link, not necessarily employees, and it does not discover the session in
other users' lists. Group shares instead appear in **Shared** for verified members.
The generic app retains public sharing for deployments without an internal group;
there is no anonymous/public-world promise implied by this policy.

Apply IdP role/group mapping before the supporting application configuration.
Publish/deploy compatible server/UI source before setting the environment variable;
older images can ignore it. Existing login tokens may hold old group snapshots,
so recipients should sign out and sign in after the mapping rollout. Confirm an
ordinary (non-admin) member can read and list a shared session, cannot edit/manage,
and loses access after revoke; a non-member and an unauthenticated request must
remain denied. Test both browser and delegated-human API paths. A UI toggle does
not prove the upstream SAML assertion contains the group; verify the actual claim
without logging whole tokens. No live IdP configuration or ACL mutation is
authorized by these setup instructions.
