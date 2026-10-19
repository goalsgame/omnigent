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
  model_reasoning:
    reasoning-model: true
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

Pi receives per-model protocols, limits and optional `model_reasoning` boolean
overrides in its signed session binding. Set reasoning support explicitly for
opaque aliases whose names do not identify the underlying model.
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
provider credentials continue to take precedence, including non-default SDK
providers and global SDK authentication. Proxy-backed sessions retain a
server-authored policy reference; model changes and forks are checked against
the server catalog without copying upstream credentials to the workspace.
Child sessions on the same inference host inherit the policy for their own
harness unless they declare personal authentication. Catalog choices remain
available while the runner is offline. Managed forks retain their provider
unless another compatible destination is explicitly selected. Start a new session to switch a
proxy-backed session to another agent.
A server and runner upgrade is
required when introducing per-model protocols or limits; relaunch existing
runners to obtain the updated binding. Retain workspace volumes during upgrades.

Vertex Gemini tool calls carry opaque thought signatures. The proxy retains native
`extra_content.google.thought_signature` fields and also exposes them as
`reasoning.encrypted` replay metadata for OpenAI-compatible clients such as Pi.
Clients must retain the replay metadata in assistant history, including after
compaction and sleep/wake; missing signatures can cause Vertex to reject tool
follow-up requests. The proxy restores signatures to the matching tool call
without caching them on the server or changing OpenRouter responses.


### Caching and session budgets

OpenRouter Claude requests without explicit cache settings receive automatic
five-minute prompt caching from the backend. This also works for clients that
register the proxy under a custom provider name. Existing `cache_control` and
Responses `prompt_cache_breakpoint` settings are preserved; other upstreams and
models are unchanged. OpenRouter requests carry a server-bound workspace/session
identifier for provider stickiness and per-session usage attribution.

Models unresolved by the shared pricing catalog can use exact public OpenRouter
model prices, cached for five minutes. Input, output, cache reads and cache writes
are counted separately, including cache writes preserved through the OpenAI Agents
SDK. For model aliases, accounting uses the upstream model recorded by the proxy
for that session. These are estimates, not provider invoices. Catalog
failures, invalid rates and context-tier prices that require per-request usage
remain unpriced.

Set `inference_proxy.session_cost_limits` to a mapping of session-owner subject
IDs to positive USD amounts, for example:

```yaml
session_cost_limits:
  oidc-machine:reviewer: 10.0
```

For listed owners, the proxy checks persisted session spend before each model
request, returning HTTP 402 when the session reaches its limit. Token usage with
no usable cost returns HTTP 503 rather than silently allowing unpriced inference.
Previously priced totals do not authorize later unpriced usage; the incomplete
pricing marker persists until the usage is reconciled. An authoritative
`cumulative_cost_usd` update must include input and output counters covering the
recorded usage to clear it; stale or cost-only updates cannot prove coverage.
Existing approval-based cost policies keep enforcing their known-spend limits
independently of this proxy guard.
Owners absent from the mapping have no proxy budget. Limits apply to each session
separately, including newly created review sessions; they do not form a daily
owner or organization budget. Usage must be reported by the runner. Reporting delays can allow multiple model calls or an entire tool loop to run
before usage is persisted, particularly with harnesses that report only at turn
completion. The proxy is a recorded-spend gate, not a strict billing ceiling. Retain
provider-side budgets as the independent billing backstop.
