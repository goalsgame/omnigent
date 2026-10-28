"""Google Drive tools that act only through grants made in the session."""

import os
from typing import Any

from omnigent.tools.base import Tool, ToolContext

_FILE_ID = {"type": "string", "description": "Drive file id from google_drive_list"}


def google_drive_tools_enabled() -> bool:
    """Whether this runner's deployment offers the Google Drive connection."""
    return os.environ.get("OMNIGENT_GOOGLE_DRIVE_AUTH") == "1"


class _DriveTool(Tool):
    TOOL_NAME = ""
    HELP = ""
    PROPERTIES: dict[str, Any] = {}
    REQUIRED: list[str] = []

    @classmethod
    def name(cls) -> str:
        """Return the runner-dispatched tool name."""
        return cls.TOOL_NAME

    @classmethod
    def description(cls) -> str:
        """Describe what the tool does and which grant it needs."""
        return cls.HELP

    def get_schema(self) -> dict[str, Any]:
        """Return the native function-call contract."""
        return {
            "type": "function",
            "function": {
                "name": self.name(),
                "description": self.description(),
                "parameters": {
                    "type": "object",
                    "properties": self.PROPERTIES,
                    "required": self.REQUIRED,
                    "additionalProperties": False,
                },
            },
        }

    def invoke(self, arguments: str, ctx: ToolContext) -> str:
        """Fail closed when no authenticated runner dispatch is available."""
        return "Error: Google Drive requires a server-connected runner"


class GoogleDriveListTool(_DriveTool):
    TOOL_NAME = "google_drive_list"
    HELP = (
        "List Google Drive files this session may use. Session participants grant individual "
        "files or their whole Drive; whole-Drive grants also make query search that Drive by "
        "name and content. If a file is missing, ask the user to add it with the Google Drive "
        "control in the session header. Anyone who can read this session sees tool output."
    )
    PROPERTIES = {
        "query": {"type": "string", "maxLength": 200, "description": "Optional search text"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
    }


class GoogleDriveReadTool(_DriveTool):
    TOOL_NAME = "google_drive_read"
    HELP = (
        "Read a granted Google Drive file. Docs return Markdown, Slides plain text, and Sheets "
        "the sheet list plus cell values (first sheet unless range is given, e.g. "
        "'Budget!A1:F50'). Plain-text files are returned as text; long content is truncated."
    )
    PROPERTIES = {
        "file_id": _FILE_ID,
        "range": {"type": "string", "maxLength": 256, "description": "Sheets A1 range"},
    }
    REQUIRED = ["file_id"]


class GoogleDriveCreateTool(_DriveTool):
    TOOL_NAME = "google_drive_create"
    HELP = (
        "Create a Google Doc from Markdown or a Google Sheet from CSV in a participant's Drive. "
        "Needs whole-Drive access with editing for this session; the new file is then granted "
        "to the session. Pass account only when several Drives allow editing."
    )
    PROPERTIES = {
        "title": {"type": "string", "minLength": 1, "maxLength": 256},
        "kind": {"type": "string", "enum": ["document", "spreadsheet"]},
        "content": {"type": "string", "maxLength": 1000000, "description": "Markdown or CSV"},
        "account": {"type": "string", "description": "Google account email to create in"},
    }
    REQUIRED = ["title", "kind"]


class GoogleDocsAppendTool(_DriveTool):
    TOOL_NAME = "google_docs_append"
    HELP = "Append plain text to the end of a Google Doc granted to this session with editing."
    PROPERTIES = {
        "file_id": _FILE_ID,
        "text": {"type": "string", "minLength": 1, "maxLength": 200000},
    }
    REQUIRED = ["file_id", "text"]


class GoogleSheetsUpdateTool(_DriveTool):
    TOOL_NAME = "google_sheets_update"
    HELP = (
        "Write rows of cell values into a Google Sheet granted to this session with editing. "
        "Each cell is text entered as if typed: numbers, TRUE/FALSE and formulas such as "
        "'=SUM(A1:A3)' are interpreted, and an empty string clears the cell."
    )
    PROPERTIES = {
        "file_id": _FILE_ID,
        "range": {"type": "string", "maxLength": 256, "description": "A1 range, e.g. 'Sheet1!A1'"},
        "values": {
            "type": "array",
            "maxItems": 10000,
            "items": {
                "type": "array",
                "maxItems": 1000,
                "items": {"type": "string"},
            },
        },
    }
    REQUIRED = ["file_id", "range", "values"]


GOOGLE_DRIVE_TOOL_TYPES = (
    GoogleDriveListTool,
    GoogleDriveReadTool,
    GoogleDriveCreateTool,
    GoogleDocsAppendTool,
    GoogleSheetsUpdateTool,
)
GOOGLE_DRIVE_TOOL_NAMES = frozenset(tool.name() for tool in GOOGLE_DRIVE_TOOL_TYPES)
