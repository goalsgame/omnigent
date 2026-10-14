# Server inference for connected hosts

An optional OpenRouter proxy lets connected hosts run supported harnesses without
receiving provider credentials. The server uses its configured `openrouter_wif`
broker. Hosts receive a session-specific inference refresh capability through
their authenticated host connection and exchange it for five-minute access tokens.

Configure the server alongside `openrouter_wif`:

```yaml
server:
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
readiness and model choices only for configured harnesses. New runner launches
receive the proxy binding; already-running runners must be relaunched. The host
stores the inference refresh capability in its data directory with private file
permissions on POSIX and inherited account permissions on Windows. Protect that
account and directory: the capability permits inference spending for its session.

## Authorization and limits

Every exchange and inference request checks the current session, runner binding,
host, live owner account generation, and machine authorization when applicable.
Deleted or archived sessions, offline/deleted hosts, changed runner bindings and
revoked machine identities reject subsequent requests. Requests already streaming
are allowed to finish. Tokens are workspace-bound and do not authorize Omnigent
control-plane operations. Provider credentials remain on the server.

The proxy forwards only `POST chat/completions` and `POST responses` to a fixed
OpenRouter endpoint. It permits only configured models, strips incoming headers,
disables redirects, limits request size, and enforces one concurrent request per
session plus the configured total per server replica. These are concurrency
limits, not a distributed quota or per-user spending budget. Configure spending
limits on the OpenRouter policy's backing key. Responses requests are stateless:
background processing, stored responses and provider conversation references are
rejected. Streaming content and usage events pass through to the harness.

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
