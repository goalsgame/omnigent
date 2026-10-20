# Pi-native tool discovery

Pi-native keeps ordinary file, shell and Omnigent session tools directly exposed.
Externally discovered MCP tools use Pi's built-in `codemode` tool when available.
The native catalog has a bounded inline description; scripts discover remaining
tools with `searchTools()` or `describeTool()` and call them through `tools`.
The complete external schema catalog is not declared on every model request.

Tool calls retain the authenticated Omnigent relay and server-side policy checks.
Calls that require approval use the existing approval flow. Native tools called
from codemode still pass through Omnigent's native policy hook. Discovery does
not execute the discovered tool. Nested calls remain visible as audit entries with
their parent call ID. Fork and cold resume replay the enclosing codemode result
rather than treating nested executions as separate model calls.

The Docker host images and CI use the Pi version in `deploy/docker/pi-version`
and the matching harness manifest and npm lockfile. Older local Pi versions, or
clients without the built-in codemode extension, retain directly exposed tools.
Upgrade those clients to the tested Pi release to use discovery.

After updating the runner, start or resume a Pi session with an MCP server. Ask
it to find a tool and perform a read-only lookup. Confirm the lookup appears in
the session history. Then verify an approval-required operation waits for the
normal Omnigent approval and a denied operation does not execute.
