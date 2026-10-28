# GitHub App setup (per-user GitHub connect)

How to register and wire the **GitHub App** that powers Omnigent's per-user
GitHub connect. Once configured, a signed-in user can **Settings → Sandbox
Integrations → Connect GitHub**, and their managed sandboxes then authenticate
`git` / `gh` as them (clone private repos, push branches, open PRs) using a
short-lived token minted from that user's authorization — never a shared PAT.

> **Screenshots** are from GitHub's **Settings → Developer settings → GitHub
> Apps** flow, walked with the example values below. The App ID / Client ID
> shown are non-secret identifiers from a throwaway example app; the client
> secret and private key are never captured.

---

## How it works (why a GitHub App at all)

- **Connect (user OAuth).** `GET /v1/connections/github/connect` signs a
  short-lived, user-bound `state` (JWT) and redirects the user to GitHub's
  authorize screen. GitHub redirects back to
  `…/v1/connections/github/callback`, which exchanges the `code` for a
  **user-to-server** access token (+ refresh token) and stores it encrypted
  (KMS) in the credential store. The `state` is signed with a subkey derived
  from the App's client secret, and the callback rebinds `state.sub` to the
  authenticated caller so it can't be replayed or cross-bound.
- **Use (in the sandbox).** Git fetches the owner's current token from the
  host-authenticated broker per operation. The managed host image's `gh` shim
  does the same before each invocation, so expiry or rotation in another
  workspace does not leave long sessions using stale credentials.
- **App, not OAuth App or PAT.** A GitHub App is an org-ownable, least-privilege
  identity whose **per-repo content access requires the App to be installed** on
  the account/org that owns the repo — so access is auditable and revocable per
  install, and there is no long-lived personal secret to leak.

> **The one gotcha:** user OAuth alone lets Omnigent *list* the repos a user can
> see, but **reading a private repo's contents/branches requires the App to be
> installed on that repo's owner** (see [Step 6](#step-6--install-the-app)). A
> connected-but-not-installed App returns `404` on private repo content.

---

## Prerequisites

- Admin on the GitHub account or org that will **own** the App (personal
  account is fine for a dev instance; use the org for a shared deploy).
- Your Omnigent server's public origin, e.g.
  `https://omnigent.example.com`. The OAuth callback must be
  reachable from a browser at `…/v1/connections/github/callback`.
- For human GitHub connections, configure a credential cipher such as Google Cloud
  KMS (`OMNIGENT_CREDENTIAL_CIPHER=gcp_kms` and
  `OMNIGENT_CREDENTIAL_GCP_KMS_KEY_ID=<full CryptoKey name>`). The server identity
  needs `roles/cloudkms.cryptoKeyEncrypterDecrypter` on that key. Install the
  backend with `pip install 'omnigent[gcp-kms]'`, or bake it into the server image
  with `docker build --build-arg OMNIGENT_EXTRAS=gcp-kms -f deploy/docker/Dockerfile .`.
  Machine App installation tokens remain in memory and do not require this store.

---

## Step 1 — Register the App

Go to **Settings → Developer settings → GitHub Apps → New GitHub App**
(`https://github.com/settings/apps/new`; for an org use
`https://github.com/organizations/<org>/settings/apps/new`).

Fill in:

| Field | Value |
| --- | --- |
| **GitHub App name** | Anything unique, e.g. `omnigent-connect` (the resolved slug becomes the install URL). |
| **Homepage URL** | Your Omnigent origin, e.g. `https://omnigent.example.com`. |
| **Description** | Optional — shown on the authorize screen. |

![Register the GitHub App: name, description, homepage](images/github-app/01-register.png)

## Step 2 — Identifying and authorizing users (OAuth)

This is the part Omnigent's connect flow depends on.

- **Callback URL / Redirect URI**: `https://<your-omnigent-origin>/v1/connections/github/callback`
  (for example: `https://omnigent.example.com/v1/connections/github/callback`).
  This **must** equal `OMNIGENT_GITHUB_APP_REDIRECT_URI` (or the value Omnigent
  derives from `OMNIGENT_DOMAIN`, which is exactly this shape).
- **Expire user authorization tokens**: **checked** — Omnigent uses the
  `refresh_token` to keep long sessions alive.
- **Request user authorization (OAuth) during installation**: leave **unchecked** —
  Omnigent runs connect as a separate, explicit step.
- **Enable Device Flow**: leave **unchecked**.

![Identifying and authorizing users: callback URL + expire tokens](images/github-app/02-oauth.png)

## Step 3 — Webhook

Omnigent's connect flow does **not** consume webhooks. **Uncheck "Active"** under
Webhook (leaving the URL blank). This avoids GitHub retrying deliveries to a
non-existent endpoint.

![Webhook: Active unchecked](images/github-app/03-webhook.png)

## Step 4 — Permissions

Under **Repository permissions**, set only what sandboxes need:

| Permission | Access | Why |
| --- | --- | --- |
| **Contents** | **Read and write** | Clone repos, and push branches for PRs. Use **Read-only** if sandboxes should only clone. |
| **Metadata** | **Read-only** (mandatory) | Auto-selected; required by GitHub. |
| **Pull requests** | **Read and write** | Open PRs from the sandbox and surface "PRs opened this session". **Read-only** if you never create PRs. |
| **Issues** | **Read-only** or **Read and write** (optional) | Read issues, or create/update them. Machine tokens must also opt in through `issues_access`. |
| **Checks** | **Read-only** (optional) | Show CI checks in the PR panel. |
| **Actions** | **Read-only** (optional) | Read the workflow runs behind GitHub Actions checks. |
| **Commit statuses** | **Read-only** (optional) | Show status-context CI in the PR panel. |

Grant Checks, Actions, and Commit statuses when the PR panel should show CI
status. Leave everything else **No access**. No **Account** or **Organization**
permissions are needed. The user-to-server token inherits these permissions,
so this list is the ceiling on what a connected sandbox can do as the user.

## Step 5 — Where can this App be installed, then Create

- **Where can this GitHub App be installed?** Pick **Only on this account** for a
  single-account/dev instance, or **Any account** if other orgs will install it.
- Click **Create GitHub App**.

You land on the new App's **General** settings page.

![Created App: General settings with App ID and Client ID](images/github-app/05-created.png)

## Step 6 — Collect credentials

On the App's **General** page:

- **App ID** — the numeric id near the top (maps to `OMNIGENT_GITHUB_APP_ID`,
  optional; only needed for app-level JWT calls, not for the user connect flow).
- **Client ID** — under *"Client secrets"* (non-secret; maps to
  `OMNIGENT_GITHUB_APP_CLIENT_ID`).
- **Client secret** — click **Generate a new client secret**, copy it **once**
  (GitHub only shows it once), store it in your secret manager. Maps to
  `OMNIGENT_GITHUB_APP_CLIENT_SECRET`.
- **Private key** *(optional)* — under *"Private keys" → Generate a private key*
  downloads a `.pem`. Only needed for app-level (installation-token) calls; the
  per-user connect flow does not require it. Maps to
  `OMNIGENT_GITHUB_APP_PRIVATE_KEY` (PEM contents) or
  `OMNIGENT_GITHUB_APP_PRIVATE_KEY_PATH` (a file path).

> Treat the client secret and private key like passwords — never commit them.
> Put them in the same secret store Omnigent already reads (e.g. the
> `omnigent-github-app` ExternalSecret on the Kubernetes deploys).

![Generate client secret and private key](images/github-app/06-secrets.png)

## Step 7 — Install the App

Open the **Install App** tab (or `https://github.com/apps/<slug>/installations/new`)
and install it on the account/org whose repositories users will access. Choose
**All repositories** or a selected set.

**This step is required for private-repo content**: without an installation on
the repo's owner, cloning or listing branches of a private repo returns `404`
even for a correctly *connected* user.

Set `OMNIGENT_GITHUB_APP_SLUG` to the App's slug (from its URL,
`github.com/apps/<slug>`) so Omnigent can render the in-product "Install" link
(`https://github.com/apps/<slug>/installations/new`).

![Install the App on an org, all repositories](images/github-app/07-install.png)

---

## Wire it into Omnigent

Set these on the **server** (the feature enables itself once client id + secret +
a resolvable redirect URI are present):

```bash
# Required — from Steps 2 and 6.
OMNIGENT_GITHUB_APP_CLIENT_ID=Iv23li...           # App "Client ID"
OMNIGENT_GITHUB_APP_CLIENT_SECRET=<generated>     # from "Generate a new client secret"

# Redirect URI — set explicitly, OR let Omnigent derive it from OMNIGENT_DOMAIN
# as https://$OMNIGENT_DOMAIN/v1/connections/github/callback (same shape).
OMNIGENT_GITHUB_APP_REDIRECT_URI=https://<your-origin>/v1/connections/github/callback

# Optional.
OMNIGENT_GITHUB_APP_SLUG=omnigent-connect          # for the in-product Install link
OMNIGENT_GITHUB_APP_ID=1234567                      # only for app-level JWT calls
OMNIGENT_GITHUB_APP_PRIVATE_KEY_PATH=/etc/omnigent/github-app.pem   # or _PRIVATE_KEY=<PEM>
```

Restart the server. If `OMNIGENT_GITHUB_APP_CLIENT_ID`/`_CLIENT_SECRET` are set
but no redirect URI can be resolved (neither `OMNIGENT_GITHUB_APP_REDIRECT_URI`
nor `OMNIGENT_DOMAIN`), the feature logs a warning and **stays disabled**.

> The at-rest encryption of stored tokens is the credential store's concern
> (`OMNIGENT_CREDENTIAL_*` / KMS), not GitHub's — see
> [`designs/CREDENTIAL_STORE.md`](../designs/CREDENTIAL_STORE.md).

---

## Verify

1. Sign in to Omnigent as a real user, open **Settings → Sandbox Integrations**.
   The **Connect GitHub** control appears when the App and encrypted connection
   store are configured (`github` appears in `/v1/info`'s `enabled_connections`).
2. Click **Connect GitHub** → GitHub's authorize screen → back to Omnigent with
   `?github=connected`. The panel now shows **Connected as `<login>`**.
3. Start a sandbox session on a **private** repo owned by an account where the
   App is **installed**; confirm the sandbox can `git clone`, push a branch, and
   (if enabled) open a PR.

If Step 3 returns `404` on a private repo while Steps 1–2 succeeded, the App is
connected but **not installed** on that repo's owner — revisit [Step 7](#step-7--install-the-app).

## Long-running GitHub CLI sessions

The managed host image installs a `gh` shim at `/usr/local/bin/gh`, with the real
CLI at `/usr/local/libexec/omnigent/gh`. The shim fetches the current owner-bound
credential before executing the CLI once. The token enters `GH_TOKEN` only in
the child environment, not arguments, stdout or an additional token file. Ambient
`GH_TOKEN`/`GITHUB_TOKEN` values cannot override a configured managed identity.
The existing periodic `hosts.yml` writer remains for compatibility; it is not
an authentication correctness guarantee. Expiry/rotation can occur between ticks.

The host stores only its broker coordinates and lesser host-scoped authority in
`~/.omnigent/github/gh-broker.json` (atomic writes, mode `0600`). Runner/native
processes need neither an inherited host-token environment nor a special PATH;
login shells and the GitHub side panel reach the image's same `gh` command.
A failed or disconnected configured broker fails closed instead of using stale
saved credentials or a shared identity. Failure to persist broker context aborts
host setup rather than reusing a prior host's authority. Offline `--version` and
`--help` diagnostics do not need the broker. CLI failures are not automatically
replayed: repeating a PR creation or another mutation could duplicate effects.

Outside managed sandboxes, or where no broker has ever been configured, the
shim preserves ordinary CLI authentication. Enterprise-token environment values
are left unchanged; the injected `GH_TOKEN` follows GitHub CLI's host scoping.
Custom host images must install the same shim and real-executable path to gain
per-invocation renewal. Deploy the updated runner image and restart/migrate
retained workspaces using the deployment's approved workspace-preservation
procedure; existing processes/images do not gain the shim automatically.

To verify after an authorized rollout, run `gh api user --jq .login` and a
read-only `gh pr list --repo OWNER/REPO` in a disposable managed session. Repeat
after a broker token refresh (or after the old token's lifetime), including when
another workspace has caused the refresh. Both should work without manual
`gh auth login` or a session restart. Check that a broker outage causes a clear
credential error and runs no GitHub command. Live account disconnect/reconnect
or synthetic fault tests require separate operator approval.

## Machine-owned sessions

Human-owned sessions use the owner's connected GitHub account. OIDC
machine-owned sessions use installation tokens only when an operator explicitly
binds that machine to an installation and repository IDs:

```yaml
github_machine_auth:
  oidc-machine:ticket-bot:
    installation_id: 123456
    repository_ids: [987654321]
    access: write
    ci_read: true
    issues_access: write
    commit_author:
      name: Ticket Bot
      email: ticket-bot@example.com
```

Each principal must already be bound by `oidc_machine_auth`. Installation and
repository IDs are public identifiers, not credentials. Obtain a repository ID
with `gh api repos/OWNER/REPO --jq .id`; use the installation ID from the App's
installation page or API. An empty repository list is rejected. `access: read`
requests read-only Contents and Pull requests permissions; `write` allows branch
pushes and PR creation. Metadata is always read-only. GitHub additionally limits
tokens to the App's installed repositories and granted permissions.
Set `ci_read: true` to include read-only access to Checks, Actions, and Commit
statuses for PR status in machine-owned sessions. The App installation must
grant all three; without `ci_read`, machine tokens retain the smaller permission
set.

`issues_access` independently controls Issues permission: `none` (the default)
requests no Issues access, `read` permits reading issues, and `write` permits
creating/updating them. Grant the corresponding permission to the App installation
as well. Having Issues permission on the App alone is insufficient: machine tokens
explicitly request only the configured subset. Enabling Issues does not widen
Contents, Pull requests, CI permissions or the repository allowlist.

`commit_author` optionally supplies both `name` and `email` for local Git commits
in that machine's managed sandboxes. Use the intended automation identity (for
GitHub attribution, an email associated with that identity); no human sign-in or
`GET /user` call is needed. Both values must be nonempty, have no surrounding
whitespace, control characters or angle brackets, and the email must have one `@`
with nonempty parts and no whitespace. The broker returns these non-secret
metadata fields alongside the credential; they are not sent to GitHub when minting
tokens and do not grant access. Without this setting, machine Git identity is left
unchanged. Human commit attribution continues to use the connected owner's email
and GitHub login. Repository-local Git configuration can still override these
sandbox-global defaults.

After an authorized configuration rollout, new tokens use `issues_access` and
new or restarted managed hosts apply `commit_author`. Changing the author setup
requires an updated runner as well as the server; already-running hosts do not
reconfigure Git automatically. Existing tokens retain their original permissions
until expiry. Do not use `gh api user` to verify a machine installation token;
that endpoint requires a user identity.

Configure the App client ID, client secret and callback, plus `OMNIGENT_GITHUB_APP_ID`
and `OMNIGENT_GITHUB_APP_PRIVATE_KEY` (or `_PRIVATE_KEY_PATH`). Human GitHub
connections require an encrypted credential store. Machine-owned installation
tokens do not. Keep the client secret and private key on the
server; neither enters a runner. Machine bindings without these prerequisites
fail server startup.

The host credential broker authenticates the sandbox's launch token and selects
credentials using its registered owner. It never accepts an owner or repository
policy supplied by the sandbox. Human owners retain their per-user token flow;
machine owners never fall back to a user connection or another machine's policy.
Installation tokens are cached only in server memory and refreshed five minutes
before expiry. The machine's current binding and non-admin eligibility are checked
on every vend, including cache hits and after an in-flight token request.

The sandbox receives a bearer installation token, so any process inside it can
use that token on the allowed repositories for its remaining GitHub lifetime.
Removing a machine binding stops future vends after configuration rollout; host
teardown also stops future vends. Neither action revokes tokens already issued by
GitHub. Keep installations and machine policies narrow.

To verify, connect GitHub as a human and create a managed session with a private
HTTPS repository workspace. Confirm clone, branch push and PR creation use that
user. Then create a machine-owned session through the authenticated session API
and confirm it uses the App bot, can access its allowed repository, and cannot
access a private repository excluded from the token policy. With `issues_access:
read` or `write`, verify `gh api repos/OWNER/REPO/issues/NUMBER` against an existing
issue; with `none`, verify Issues access is denied. With a configured
`commit_author`, create a disposable local repository, run `git commit --allow-empty
-m 'test: verify machine author'`, and check `git log -1 --format=fuller` shows the
configured author and committer. This needs no push or issue creation. A machine with no
GitHub binding must receive no GitHub credential, even if a user connection was
previously stored under that subject.
