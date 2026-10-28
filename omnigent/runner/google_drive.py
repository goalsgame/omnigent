"""Authenticated REST transport for the Google Drive tools."""

from __future__ import annotations

import json
import re
from urllib.parse import quote

import httpx

from omnigent.util.json_types import JsonObject

_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


def _file_id(args: JsonObject) -> str:
    value = args.get("file_id")
    if not isinstance(value, str) or not _FILE_ID.fullmatch(value):
        raise ValueError("file_id must be a Google Drive file id from google_drive_list")
    return quote(value, safe="")


async def execute_google_drive_tool(
    tool_name: str,
    args: JsonObject,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """Run a Drive tool against the server, which enforces the session's grants."""
    if server_client is None or conversation_id is None:
        return json.dumps({"error": "Google Drive requires a server-connected session"})
    if not _SESSION_ID.fullmatch(conversation_id):
        return json.dumps({"error": "Google Drive requires a server session"})
    base = f"/v1/sessions/{conversation_id}/google-drive/agent/files"
    try:
        if tool_name == "google_drive_list":
            params = {"query": str(args.get("query") or "")}
            if args.get("limit") is not None:
                params["limit"] = str(args["limit"])
            response = await server_client.get(base, params=params, timeout=60.0)
        elif tool_name == "google_drive_read":
            params = {"range": str(args["range"])} if args.get("range") else None
            response = await server_client.get(
                f"{base}/{_file_id(args)}", params=params, timeout=60.0
            )
        elif tool_name == "google_drive_create":
            response = await server_client.post(base, json=args, timeout=60.0)
        elif tool_name == "google_docs_append":
            response = await server_client.post(
                f"{base}/{_file_id(args)}/append", json={"text": args.get("text")}, timeout=60.0
            )
        elif tool_name == "google_sheets_update":
            response = await server_client.put(
                f"{base}/{_file_id(args)}/values",
                json={"range": args.get("range"), "values": args.get("values")},
                timeout=60.0,
            )
        else:
            return json.dumps({"error": "Unknown Google Drive tool"})
    except ValueError as exc:
        return json.dumps({"error": str(exc)})
    except httpx.HTTPError:
        return json.dumps({"error": "Google Drive request failed; retry", "outcome_unknown": True})
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else None
        error = body.get("error") if isinstance(body, dict) else None
        if detail is None and isinstance(error, dict):
            detail = error.get("message")
        if isinstance(detail, dict):
            detail = detail.get("message")
        if isinstance(detail, list):
            detail = "Invalid arguments: " + "; ".join(
                str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                for item in detail
            )
        return json.dumps(
            {"error": detail or f"Google Drive request failed (HTTP {response.status_code})"}
        )
    return json.dumps(body)
