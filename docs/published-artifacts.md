# Published artifacts

Agents can publish downloadable files or self-contained UTF-8 HTML pages using
`sys_publish_artifact`. Each publication has an immutable ID, SHA-256 content
hash and stable `/artifacts/{id}` link. Publishing a new revision creates a new
ID; it never changes the bytes behind an existing link.

Publications live in the server's artifact storage and SQL metadata catalog,
independently of the source session. Suspending or deleting the session does
not delete its published outputs. A publication starts private to the identity
that publishes it. Copying a link does not grant access.

## Publishing

Ask a server-connected agent to create an output in its workspace and call:

```json
{"path":"output/report.html","title":"Quarterly report","kind":"html"}
```

Use `kind: "file"` for other downloads. The native tool confines reads to the
session workspace and caps its upload at 50 MiB. Publishing requires edit access
to the source session. Human and machine identities publish as themselves.
The tool does not grant sharing permissions.

The REST equivalent is a multipart request:

```sh
curl -H "Authorization: Bearer $OMNIGENT_TOKEN" \
  -F 'title=Quarterly report' -F 'kind=html' \
  -F 'file=@output/report.html' \
  "$OMNIGENT_URL/v1/sessions/$SESSION_ID/artifacts"
```

The response includes `id`, `viewer_url`, `download_url`, `sha256` and
`expires_at`. The owner can find their latest 100 publications in **Artifacts**
in the sidebar or command palette. Shared recipients open the publication link.

## Viewing and sharing

The viewer supports downloading, copying the link, deleting, and granting or
revoking read access to exact user identities or OIDC group names. Group grants
use verified, unexpired membership claims, with the same exact-name semantics
as session sharing. Only the publication owner can change its grants or delete
it. New grants honor the server's sharing-disabled policy; revocation remains
available. Anonymous/public grants are not supported.

HTML runs inside an iframe with an opaque origin. Inline JavaScript and CSS
work; the page cannot read the parent application, browser storage or call
Omnigent APIs. External assets and fetch requests are blocked. Inline images,
fonts and media may use data URLs. Bundle scripts, styles and data into the HTML
file before publishing; multi-file sites, module imports and external APIs are
not supported. No extension or agent-tool bridge is exposed to published HTML.

## Retention and storage

The server configuration controls publication limits separately from sandbox
retention:

```yaml
published_artifacts:
  public_url: https://assistant.example.com
  max_bytes: 52428800
  max_html_bytes: 5242880
  retention_days: 90
  max_retention_days: 365
```

Set `public_url` to the public HTTPS origin when runners reach the server through
an internal service address. The application base path is appended automatically.
If omitted, API links are relative and the native tool resolves them against its
server address. The remaining values shown above are the defaults.

A publish request may set `retention_days` within the configured maximum.
Expiry or explicit deletion immediately denies subsequent reads. A periodic,
idempotent collector removes expired/deleted blobs and abandoned uploads; failed
storage cleanup is retried. Already downloaded bytes are outside server control.

Blobs use a separate `published/{workspace}/{id}` namespace in the configured
artifact store. Local, GCS, S3 and other existing storage backends share this
contract. A pending SQL record precedes upload; only completed uploads are
visible. Deployments must retain this namespace for at least the configured
publication lifetime. Sandbox/session cleanup must not sweep the namespace.

## API

| Method and route | Authority |
| --- | --- |
| `POST /v1/sessions/{id}/artifacts` | Session editor; creates a private publication |
| `GET /v1/artifacts` | Latest 100 publications owned by the caller |
| `GET /v1/artifacts/{id}` | Owner or explicitly granted reader |
| `GET /v1/artifacts/{id}/content` | Same; forced download |
| `GET /v1/artifacts/{id}/preview` | Same; isolated HTML only |
| `PUT /v1/artifacts/{id}/permissions` | Owner; replace the read-grant list |
| `DELETE /v1/artifacts/{id}` | Owner; immediate revocation |

Permissions requests use `{"grants":[{"principal_type":"group",
"principal":"engineering"}]}` or `principal_type: "user"`. An empty list
revokes all explicit readers. The owner remains authorized. Every read checks
current identity, tenant, expiry and grants; unauthorized IDs return 404.
