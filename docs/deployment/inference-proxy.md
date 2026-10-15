# Server inference for connected hosts

An optional inference proxy lets connected hosts run supported harnesses without
receiving provider credentials. The default upstream uses the configured
`openrouter_wif` broker. Hosts receive a session-specific inference refresh capability through
their authenticated host connection and exchange it for five-minute access tokens.

Configure the server alongside `openrouter_wif`:

```yaml
inference_proxy:
  public_url: https://omnigent.example.com
  signing_key_env: OMNIGENT_INFERENCE_SIGNING_KEY
  models:
    - provider/model-id
  harnesses:
    - openai-agents
  max_request_bytes: 8388608
  max_concurrent_requests: 32
```

Use a random secret of at least 32 bytes for the signing key, shared by server
replicas. Rotating it revokes outstanding proxy credentials and requires runner
relaunches. The public URL must be an HTTPS origin. The first allowed model is
used as the provider default. `pi-native` can also be enabled; native CLI account
flows are otherwise unaffected. This configuration is disabled when omitted.

Opt a host in before starting it. In PowerShell:

```powershell
$env:OMNIGENT_SERVER_INFERENCE = "1"
omnigent host --server https://omnigent.example.com --no-open
```

For a POSIX shell:

```sh
OMNIGENT_SERVER_INFERENCE=1 omnigent host --server https://omnigent.example.com
```

The host still needs the harness installed. Opted-in hosts advertise server-backed
readiness and model choices only for configured harnesses that report missing
authentication. Personal provider credentials, explicit `executor.auth`, legacy
executor profiles and saved direct bindings retain their direct routing. Runner
launches needing server authentication receive the proxy binding; already-running
runners must be relaunched. CLI `/clear` and `/new` stop an opted-in daemon
runner; the next send launches a fresh process in the same host workspace with
credentials bound to the new session. REPL forks also obtain their own runner;
switching to a host-bound session reuses its valid binding or launches a fresh
runner when it is unbound. The host
stores the inference refresh capability in its data directory with private file
permissions on POSIX and inherited account permissions on Windows. Protect that
account and directory: the capability permits inference spending for its session.
Before session initialization, runners verify transient proxy configuration
against the authoritative server, including the issued model policy, session,
runner and refresh grant. Saved direct configurations retain exact-match
validation. Reconnecting runners repeat this verification.
On opted-in hosts, children launch independent runner processes in their inherited
workspace so each harness retains its own provider configuration.
JSON and uploaded-agent sessions use the same placement policy. Generic side chats
also receive independent runners and inference bindings so source and side turns
can stream concurrently, subject to the shared server capacity. Recovery keeps
these runners separate. Shared runner bindings are verified against authoritative
same-runner lineage, including independently launched intermediate ancestors and generic side-chat sources.
Grant files and generated runtime configs are removed when the runner stops or
exits, or a launch fails.

## Authorization and limits

Every exchange and inference request checks the current session, runner binding,
host, live owner account generation, and machine authorization when applicable.
Deleted or archived sessions, offline/deleted hosts, changed runner bindings and
revoked machine identities reject subsequent requests. Requests already streaming
are allowed to finish. Tokens are workspace-bound and do not authorize Omnigent
control-plane operations. Provider credentials remain on the server. Proxy URLs
carry a signed workspace/session selector, verified before workspace-scoped
authorization lookups. Access tokens must match the selected workspace.

The proxy forwards only `POST chat/completions`, `POST responses` and native
`POST v1/messages` to operator-configured upstreams. It permits only configured models, strips incoming headers,
disables redirects, limits request size, and enforces one concurrent request per
session plus the configured total per server replica. Capacity is reserved before
reading the request body and released on upload failure, cancellation or response
completion. These are concurrency limits, not a distributed quota or per-user spending budget. Configure spending
limits on the OpenRouter policy's backing key. Responses requests are stateless:
background processing, stored responses and provider conversation references are
rejected. Streaming content and usage events pass through to the harness. A
shared upstream client reuses connections and closes during server shutdown.
Provider cookies are not retained between requests.

`GET models` returns the configured catalog. Upstream HTTP errors return sanitized
errors rather than provider response bodies. A disconnected client cancels the
upstream request and releases its capacity slot. Configure ingress timeouts to
accommodate long inference streams; disable response buffering.

## Verification

With the server configured and an opted-in host connected, create a session using
an enabled harness and an allowed model, with no local provider credentials. Ask
it for a short response and then a tool action. Verify both complete, then cancel
a streaming turn and verify another turn succeeds. Confirm an unsupported model
is rejected and an archived session cannot refresh its inference credential.

## Multiple upstreams

Without `upstreams` and `model_routes`, inference uses the existing OpenRouter
WIF broker. To route models independently, configure both mappings. Every allowed
model must have exactly one explicit route; there is no fallback after a provider
rejects a request. Public model IDs are aliases and need not match provider IDs.

```yaml
inference_proxy:
  public_url: https://omnigent.example.com
  signing_key_env: OMNIGENT_INFERENCE_SIGNING_KEY
  models: [code-model, reasoning-model, fast-model]
  harnesses: [pi-native, openai-agents]
  upstreams:
    router:
      kind: openrouter
    claude:
      kind: vertex-anthropic
      project: example-project
      location: eu
    gemini:
      kind: vertex-openai
      project: example-project
      location: europe-west1
  model_routes:
    code-model: {upstream: router, model: provider/model-id}
    reasoning-model: {upstream: claude, model: claude-model-id}
    fast-model: {upstream: gemini, model: google/gemini-model-id}
  model_limits:
    reasoning-model: {context_window: 200000, max_output_tokens: 64000}
```

Install the server with the `vertex` extra. Vertex upstreams use Application
Default Credentials, including GKE Workload Identity, and refresh access tokens
on the backend. Grant the server identity `aiplatform.endpoints.predict` and
`serviceusage.services.use` in each target project, enable the Vertex AI API,
and enable required partner models in Model Garden. Model availability and IDs
depend on the selected location. Google endpoints are derived from project and
location; an ADC token cannot be sent to an operator-supplied URL. OpenRouter
upstreams require `openrouter_wif`; Vertex-only configurations do not.

Pi receives the per-model protocol and limits in its signed session binding.
Native Claude messages, tools, images, thinking and SSE pass through; the backend
adapts only Vertex's URL, model placement and `anthropic_version`. OpenAI Agents
receives only OpenAI-compatible routes; Vertex's OpenAI endpoint accepts chat
completions, not Responses. Unsupported protocol/model pairs fail before fetching
upstream credentials. Select model limits supported by the actual endpoint.

Other OpenAI-compatible providers use `kind: openai`, an HTTPS `base_url`, and
`api_key_env` naming a server environment variable. Literal credentials are not
accepted. Clients cannot select URLs, projects or upstream credentials. Configure
provider budgets and quotas separately; Omnigent's limits remain per replica.

For managed sandboxes, set `inference_proxy.managed_sandboxes: true` to expose
the proxy model catalog before launch. Sandboxes must also opt in with
`OMNIGENT_SERVER_INFERENCE=1` and omit direct provider bindings from their host
configuration. Otherwise their configured
provider credentials continue to take precedence. A server and runner upgrade is
required when introducing per-model protocols or limits; relaunch existing
runners to obtain the updated binding. Retain workspace volumes during upgrades.
