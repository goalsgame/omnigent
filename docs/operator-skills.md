# Operator-managed skills

A server can attach portable skill files to selected extra built-in agents without
putting workflow instructions into each agent's prompt. Skills use the existing
bundle format, parser, user skill menu and harness discovery paths. They do not
change tool permissions, credentials, policy approvals, harness or model.

Mount extra agent YAML files or bundle directories and list their paths in
`OMNIGENT_BUILTIN_AGENT_DIRS`, separated by the platform path separator. In the
server configuration selected by `OMNIGENT_CONFIG`, add:

```yaml
operator_skills:
  library:
    code-review:
      SKILL.md: |
        ---
        name: code-review
        description: Review changes for actionable defects.
        ---
        Read the requested diff and verify relevant caller contracts.
        For detailed checks, read [the checklist](references/checklist.md).
      references/checklist.md: |
        Check input validation and failure recovery at the changed boundaries.
  agents:
    assistant:
      - code-review
```

Skills can also be mounted as files. Each entry in `library_dirs` is an absolute
directory whose immediate subdirectories are skills: `<dir>/<skill-id>/SKILL.md`
plus any reference files beneath it. Files are read as UTF-8 text at startup
and validated exactly like inline `library` entries; dot-prefixed names are
skipped, so a Kubernetes ConfigMap mount per skill works as a library
directory. Traversal follows links within a skill, rejects links back to an ancestor and visits at most 512 directories and lists at most 4096 entries across all library directories, stopping as soon as a limit is passed. A skill ID may appear in only one of `library` and `library_dirs`.

```yaml
operator_skills:
  library_dirs:
    - /etc/omnigent/skills
  agents:
    assistant:
      - code-review
```

`assistant` must be the name of a mounted extra entry: a YAML file's stem or a
bundle directory's name. The mapping does not target packaged built-in agents or
user-installed agents. Existing catalog restrictions still apply: mapping a skill
does not enable an agent excluded by `agents.allowed_names`. Each skill needs `SKILL.md` with YAML frontmatter containing
`name` and `description`. Its frontmatter name must equal its library skill ID
so Pi and SDK invocation names agree. Relative supporting resources are retained in its own
`skills/<skill-id>/` directory. Native Pi receives explicit `--skill` paths. SDK
agents use the usual skill tools. Skill activation never grants execution rights.

The agent YAML's existing `skills:` field is a discovery/filter setting, not a list
of files to install. Operator skills do not modify it. Existing harness skill
filters still apply. Agent prompts and runtime configuration remain unchanged.

Configuration is validated before server startup, including unknown agents/skills,
invalid skill entrypoints, unsafe file paths, linked bundled skills and collisions
with skills already in a source bundle. Skill IDs are lowercase hyphenated names, at most 64 characters.
Mapped agent names use the existing `[a-zA-Z0-9_-]+` agent-name format. Files must be UTF-8 text, without NUL,
using relative POSIX paths without traversal, backslashes or drive prefixes.
Case-colliding paths are rejected. Do not put secrets in skill files.

Limits are 128 skills, 128 agent mappings, 512 files total, 128 KiB per file and
900 KiB for the serialized configuration. Supporting executable/binary installers
are not provided; skill resources are text files, not a software packaging system.

On restart, the selected files become part of each agent's content-addressed bundle.
Changing a selected instruction/resource or removing its mapping refreshes that
bundle and both cache tiers while preserving the built-in agent ID. Unselected
library changes do not affect an agent. Omitting `operator_skills` preserves normal
extra-agent registration and removes previously injected files from rebuilt
bundles. Existing bundled skills are never silently overwritten.

To verify, restart a development server with one mapped extra agent. Confirm the
skill appears in the agent catalog and a new session can load its instructions
and reference file. Change the reference text and restart; the updated text should
be readable under the same agent ID. Remove the mapping and restart; the injected
skill should disappear while the agent's prompt and other configuration remain.
