# Separate server and workspace telemetry identity

`OMNIGENT_TELEMETRY_ENABLED=true` opts into runtime telemetry. Standard
`OTEL_RESOURCE_ATTRIBUTES` defines deployment resource attributes.

Managed hosts, runners and harnesses may be released independently of the server.
Set `OMNIGENT_OTEL_RUNNER_RESOURCE_ATTRIBUTES` to their own complete resource
attribute string, such as:

```text
deployment.environment.name=production,service.namespace=agents,service.version=runner-release
```

When initializing `omni-host`, `omni-runner` or `omni-harness`, Omnigent uses this
value as `OTEL_RESOURCE_ATTRIBUTES` before installing trace, metric and log
providers. Child processes inherit the selected resource identity; the existing
`OMNIGENT_OTEL_` environment passthrough preserves the override. `omni-server`
and custom-named services keep the standard value. An unset override preserves
existing behavior, and telemetry-disabled processes remain inert. An explicitly
empty override clears the inherited attributes for those workspace components.

Keep the server's version in its standard `OTEL_RESOURCE_ATTRIBUTES`. Pass only
the runner-specific attribute variable through the managed workspace template:
changing a server version then need not change the workspace admission profile.
Include desired environment/namespace attributes in the override; it replaces,
not merges with, the standard value. These values must contain no credentials
or user payloads.

The deployment must establish server/runner protocol compatibility before reusing
an older runner image. Independent identity is observability configuration, not
a protocol-compatibility guarantee. Adopting this variable changes existing
workspace environment once and requires the deployment's normal profile/migration
procedure; this document does not authorize any live workspace changes.
