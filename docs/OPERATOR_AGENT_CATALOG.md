# Operator agent catalog

The server accepts an optional template allowlist in `OMNIGENT_CONFIG`:

```yaml
agents:
  allowed_names:
    - custom-pi
    - custom-claude
```

Only matching template names appear in agent discovery and can be selected for
new sessions. Packaged agents outside the list are neither seeded nor refreshed.
An empty list exposes no templates. Omitting the setting preserves the full
catalog. Names are exact and case-sensitive; the list does not create agents.
Supply their YAML definitions through `OMNIGENT_BUILTIN_AGENT_DIRS` as usual.

Existing database records remain available to their sessions and history.
Changing the list does not delete sessions, templates, or workspace data.
This setting curates templates; it does not disable session-scoped agents or
user-authored session bundles.
