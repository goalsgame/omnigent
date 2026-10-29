"""Agent search across past sessions everyone in the current session can read."""

import os
from typing import Any

from omnigent.tools.base import Tool, ToolContext


def session_search_tool_enabled() -> bool:
    """Whether this runner's deployment offers the session search tool."""
    return os.environ.get("OMNIGENT_SESSION_SEARCH_TOOL") == "1"


class SearchSessionsTool(Tool):
    """Rank past sessions by title, message text and digest through the server."""

    @classmethod
    def name(cls) -> str:
        """Return the runner-dispatched tool name."""
        return "search_sessions"

    @classmethod
    def description(cls) -> str:
        """Describe the search and its audience limit."""
        return (
            "Search earlier Omnigent sessions to recall decisions, discussions, open questions "
            "and findings. Ask in plain language (e.g. why did we choose gVisor for sandboxes) "
            "or use keywords with quoted phrases, OR and -exclusions. Results only include "
            "sessions that every participant of this session can already read, because they "
            "all see this tool's output. Returns session ids with matching digest entries and "
            "message snippets; read more with sys_session_get_history."
        )

    def get_schema(self) -> dict[str, Any]:
        """Return the native function-call contract."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "A question or keywords, e.g. how do sleeping "
                                'workspaces migrate, or "warm pool" -staging'
                            ),
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum sessions to return, 1-50. Default 10.",
                        },
                    },
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Fail closed when no authenticated runner dispatch is available."""
        return "Error: session search requires a server-connected runner"


SESSION_SEARCH_TOOL_NAMES = frozenset({SearchSessionsTool.name()})
