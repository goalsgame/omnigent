"""Publish workspace outputs through the authenticated server artifact API."""

from typing import Any

from omnigent.tools.base import Tool, ToolContext


class PublishArtifactTool(Tool):
    """Publish an immutable file or self-contained HTML with a durable private link."""

    @classmethod
    def name(cls) -> str:
        """Return the framework tool name."""
        return "sys_publish_artifact"

    @classmethod
    def description(cls) -> str:
        """Describe retention, preview constraints and initial visibility."""
        return (
            "Publish a workspace file for downloading or a self-contained UTF-8 HTML page "
            "for interactive viewing. HTML must inline its scripts, styles and data; network "
            "API requests and external assets are blocked. Returns a durable authenticated link "
            "independent of this session. Initially private to the publishing identity; "
            "the owner can share with named users/groups in the artifact viewer."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the publication schema without any implicit sharing authority."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "File path relative to the workspace",
                        },
                        "title": {"type": "string", "minLength": 1, "maxLength": 256},
                        "kind": {"type": "string", "enum": ["file", "html"], "default": "file"},
                        "retention_days": {"type": "integer", "minimum": 1},
                    },
                    "required": ["path", "title"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Require the runner path, which carries the caller's session authority."""
        return "Error: publishing artifacts requires a server-connected runner"
