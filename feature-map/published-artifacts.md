# Published artifacts

Agents publish durable downloads or interactive, self-contained HTML reports.
Owners share stable links with users and groups independently of session lifetime.

## Sub-features

- `publish`: a workspace output receives an immutable link and expiry.
- `catalog`: owners find outputs through Artifacts in navigation or the command palette.
- `viewer`: authorized readers preview HTML or download exact published bytes.
- `full-page`: HTML viewers open a viewport-filling isolated view in a new tab.
- `sharing`: owners add and revoke named user/group read access.
- `isolation`: artifact scripts cannot access application state or APIs.
- `retention`: deleting a source session preserves publications; expiry/deletion denies reads.

## How to get to it (user POV)

- Agent: request `sys_publish_artifact` with a workspace path, title and kind.
- API: publish a multipart file through a session's artifacts endpoint.
- Web/desktop/mobile: open the returned `/artifacts/{id}` link.
- Navigation: choose Artifacts from the sidebar or Go to Artifacts in the command palette.
- Owner viewer: expand Sharing to grant or remove user/group read access.
- HTML viewer: choose Open full page to open `/artifacts/{id}?view=full` in a new
  tab; the same URL can be loaded or reloaded directly and rechecks access.

## Driving it with the repro environment

Preconditions: use an isolated server with SQL metadata and artifact storage;
HTML reports must contain their own scripts/styles/data. No provider access is needed.

- API, permissions and independent lifetime: `tests/server/test_published_artifacts.py`.
- Real-server native publication and session deletion: `tests/e2e/test_published_artifacts.py`.
- Agent publication and workspace boundaries: `tests/runner/test_publish_artifact_tool.py`.
- Viewer/list/permission forms: `web/src/pages/ArtifactsPage.test.tsx`.
- Full-page URL, new-tab link and base-path behavior: `web/src/pages/ArtifactsPage.test.tsx`.
- Real browser, own environment: `tests/browser_ui/test_published_artifacts.py`.
  Run with `--browser-ui-skip-build` after building this checkout's web bundle.
- Human check: ask Pi to publish an HTML report with an interactive button. Open
  its link, choose Open full page and verify the report fills the new tab and
  still interacts without application/API access. Return to details, grant a
  group read access, verify another member can view
  it, revoke access and verify a fresh open is denied. Delete the source session
  and confirm the owner's publication still loads.

## Gotchas

- Copying a link grants no access; a session reader does not automatically read
  publications. Publication grants are independent of session grants.
- HTML is self-contained; external assets, API calls and multi-file sites are unsupported.
- Publishing a new version creates a new immutable link.
- The server's publication retention and artifact-store lifecycle determine lifetime,
  independently of sandbox PVC cleanup.
- Browser-contract tests use a sealed mock backend. API tests verify actual SQL
  and blob behavior; neither substitutes for a deployment smoke test.
