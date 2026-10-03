# Session startup timings

Set `OMNIGENT_STARTUP_TIMING=1` on the server and sandbox host/runner images or
process environments to enable diagnostic startup records. It is disabled by
default. For managed Kubernetes sandboxes, include the environment variable in
`sandbox.kubernetes.env` when forwarding it from the server. An image-level
setting also enables the preparation container before assignment.

Every record starts with `startup_timing` and contains JSON with a phase, `begin`
or `end` event, wall-clock `time_ns`, process ID and span ID. End records include
monotonic `duration_ms` and an outcome: `returned` means the call returned normally,
including an HTTP error response; `raised` means an exception or cancellation
escaped. Use existing response/error logs to determine whether initialization
succeeded. Credentials, request bodies, commands and exception messages are
excluded.

Nested records include `parent_span_id`; server worker threads inherit their
parent span. Session and host IDs join records across the server, preparation
container, host and runner. A span covers its nested work, so do not add parent
and child durations together. Wall-clock timestamps help join processes but
cannot replace monotonic durations when clocks differ.

The records cover session creation and persistence, background launch, launcher
construction and SDK/config loading, warm-pool reads and claim creation,
assignment checks, Pod validation, deadline updates, exec connection opening and
completion, workspace preparation, host CLI/authentication, tunnel upgrade,
runner launch/fork, agent-spec resolution, harness spawning, MCP discovery,
native-terminal creation and initialization. `runner.native_input_ready` is a
checkpoint when the terminal watcher first observes input readiness. Warm-pool
provisioning spans include controller wait and polling; controller and Kubernetes
events are needed to distinguish assignment, scheduling, image pulls and volumes
when no spare was available.

To measure startup, create an empty-workspace session and collect logs from the
server, the allocated Pod's preparation and host containers, and its runner log.
Host container stderr includes early CLI spans before process-file logging starts.
The host forwards the tracing switch through its runner environment allowlist.
Wait for `runner.native_input_ready` before deleting a disposable session.
Runner connection alone does not mean initialization or input readiness finished.
Compare the first session after server restart with repeated warm allocations.

Warm spares import host connection code and probe installed CLI binary versions
before advertising runtime readiness. Successful version probes use the existing
executable-signature cache; replacing a binary causes a new probe. Credential
availability is checked after assignment against the current configuration.
Managed hosts with an injected ID, name and launch token skip the Databricks
browser-auth preflight; server registration still validates their launch token.

Pi terminal preparation includes `pi.*` spans for launch configuration, token
resolution, built-in tool schemas, MCP schemas, extension files, resume lookup,
version detection, provider configuration and terminal creation. These are nested
inside `runner.launch_native_terminal`; MCP connection spans may be emitted by
the server handling discovery.
