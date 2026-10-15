# Lore checkouts

Kubernetes and Agent Sandbox sessions accept `lores://lore.example.com/game#main@369`.
A branch without a revision selects its latest revision. A 64-digit hash is used
unchanged. Git URLs retain existing behavior; Git clone options do not apply to
Lore. Other managed providers reject Lore before provisioning. Local hosts can
continue using their own Lore login and existing checkout.

Install a Lore CLI supporting `--identity-token`, `clone --view` and
`clone --revision` in the runner image. Configure the server:

```yaml
oidc_lore:
  upstream_url: https://lore.example.com
  audience: lore
  scope: openid lore-access
  machine_client_id: machine-exchange
  machine_client_secret_env: LORE_EXCHANGE_CLIENT_SECRET
  machine_scope: openid lore-access
  repositories:
    lores://lore.example.com/game:
      view: |
        /Content
        /Binaries
        /Intermediate
  machine_principals: []
```

`view` is native Lore view-file text: patterns exclude paths and `!` rules
reinclude them. Omit it to clone all files. Size the disk for the intended view.
Allowed URLs must match the upstream host and port, and cannot contain
credentials, query strings, fragments, escaping or traversal. Session refs are
not included in the repository allowlist.

The identity provider must allow renewable token exchange. Tokens must retain
the owner's subject, have the configured audience and identify the exchanging
client. Lore and its auth bridge must trust that issuer, audience and client,
and receive the claims their authorization policy requires. An Omnigent login
token does not automatically meet those requirements. Sign in again or renew
API delegation after enabling Lore.

Lore grants are encrypted separately from MCP grants and refreshed under the
credential store's rotation lock. Machines require a current Omnigent identity
binding and an explicit entry in `machine_principals`; there is no shared-bot
fallback. The empty list disables machines.

Preparation clones into staging before publishing the checkout. Retained
checkouts must match the recorded path, repository and requested ref, and retain
`.lore` metadata. They are never automatically synchronized, reset or reconfigured
on wake. Unrelated or changed directories are not overwritten.

Managed POSIX hosts install a wrapper that obtains a fresh short-lived token for
each Lore command through a loopback relay. The host capability stays in host
memory; helper configuration contains no tokens. IdP refresh tokens stay on the
backend. Local hosts are untouched, including Windows hosts with their own login.
Login shell profiles must retain `$HOME/.omnigent/lore/bin` in `PATH` when
the managed wrapper exists. The CLI receives its short-lived token through `--identity-token`, so processes
inside the sandbox can inspect it while the command runs. The sandbox is the
trust boundary. A long-running CLI command cannot renew its token midway through
execution; validate intended checkout duration against token lifetime.

The repository list limits credential delivery, not Lore permissions. A token
can exercise all permissions Lore grants its identity until expiration. Enforce
repository and read/write boundaries in Lore or its auth bridge before enabling
restricted bots.

## Verification

Create a session at an allowed URL/ref and read a known included file. Confirm
excluded paths are absent. Run `lore status`, write a file, suspend and wake the
session, then run `lore status` again and confirm the file survives. Expire a
delegation and verify the next command refreshes it. Revoked identities must fail
without substituting another identity. Check the largest intended view against
disk capacity and token lifetime before enabling users.

File and shell tools work with these workspaces. Git worktrees, GitHub PR panels
and repository-wide Git change tracking do not acquire Lore equivalents.
