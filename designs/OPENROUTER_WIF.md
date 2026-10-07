# OpenRouter workload identity federation

Managed sandboxes can use short-lived OpenRouter inference credentials minted
from the server's workload identity. Configure an organization issuer and a
federation policy in OpenRouter first. The policy binds the issuer, subject and
audience to an active organization-owned workspace API key. The key's secret
value is never needed by Omnigent.

The server configuration enables the broker:

```yaml
openrouter_wif:
  policy_id: <federation-policy-id>
  audience: https://openrouter.ai
```

By default, the server obtains a Google ID token from its metadata server. The
OpenRouter issuer must be `https://accounts.google.com`; its policy subject is
the Google service account's numeric unique ID. The server needs metadata access
and its existing Google workload identity binding. Sandbox metadata access is
not required.

Other identity providers can supply an automatically rotated JWT file using
`openrouter_wif.subject_token_file: /var/run/identity/token`. The configured
OpenRouter policy must accept that file's issuer, subject and audience. Omnigent
rereads the file on every exchange; the runtime owns source-token rotation.

Configure the managed host's provider families:

```yaml
sandbox:
  host_config:
    providers:
      openrouter:
        kind: gateway
        default: [anthropic, openai, pi]
        anthropic:
          base_url: https://openrouter.ai/api
          auth_command: python3 -m omnigent.host.inference_credential
          auth_refresh_interval_ms: 60000
        openai:
          base_url: https://openrouter.ai/api/v1
          wire_api: chat
          auth_command: python3 -m omnigent.host.inference_credential
          auth_refresh_interval_ms: 60000
```

Keep the existing model and harness configuration. Native Codex uses the
Responses protocol; its model and endpoint must support that protocol.

The helper authenticates to `/v1/hosts/{host_id}/credentials/openrouter` using
the managed host's launch token. Every fetch revalidates that token before
returning a bearer. Responses have `Cache-Control: no-store`. All authorized
managed hosts on this server share the configured workload policy, including
human-owned and machine-owned sessions. This does not delegate a user's personal
OpenRouter identity. Other applications can reuse the organization issuer and
configure their own federation policies without depending on Omnigent.

Tokens are cached only in server memory, with concurrent exchanges coalesced
per replica. Refresh starts when less than two minutes remain; tokens with no
more than two minutes of validity are rejected. Claude and Codex must refresh
their helper more frequently than this margin. Pi resolves the command per
request. Exchange errors fail closed with a sanitized 503; there is no fallback
to a static key. Source tokens and response bodies are not logged.

Upgrade both server and managed-host images before enabling the provider
command. Host startup records the launch coordinates in a private file in its
home directory; it never persists the OpenRouter bearer. Already-running hosts
need to restart on the compatible image to create those coordinates. Existing
warm Sandboxes also need a compatible profile according to their launcher's
upgrade procedure; replacing a warm-pool template only upgrades unclaimed
spares.

Verification: start a fresh managed session and send a prompt, leave it open
for more than 15 minutes, then send another prompt. Repeat after suspending and
waking the session. All turns must succeed without a static OpenRouter API key.
The same checks apply to each enabled harness. Pausing an OpenRouter policy
blocks subsequent exchanges; disabling its target key blocks inference even
with an already-issued token.

See [OpenRouter WIF documentation](https://openrouter.ai/docs/guides/overview/auth/workload-identity-federation).
