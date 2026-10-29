"""Authenticated REST transport for the session search tool."""

from __future__ import annotations

import json
import re

import httpx

from omnigent.util.json_types import JsonObject

_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")


async def execute_session_search_tool(
    args: JsonObject,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """Search through the server, which limits results to the session's audience."""
    if server_client is None or conversation_id is None:
        return json.dumps({"error": "Session search requires a server-connected session"})
    if not _SESSION_ID.fullmatch(conversation_id):
        return json.dumps({"error": "Session search requires a server session"})
    query = args.get("query")
    if not isinstance(query, str) or not query.strip():
        return json.dumps({"error": "query must be a non-empty string"})
    params = {"q": query}
    limit = args.get("limit")
    if limit is not None:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            return json.dumps({"error": "limit must be an integer from 1 to 50"})
        params["limit"] = str(limit)
    try:
        response = await server_client.get(
            f"/v1/sessions/{conversation_id}/search", params=params, timeout=30.0
        )
    except httpx.HTTPError:
        return json.dumps({"error": "Session search request failed; retry"})
    try:
        body = response.json()
    except ValueError:
        body = None
    if response.status_code >= 400:
        error = body.get("error") if isinstance(body, dict) else None
        message = error.get("message") if isinstance(error, dict) else None
        if message is None and isinstance(body, dict) and isinstance(body.get("detail"), str):
            message = body["detail"]
        return json.dumps(
            {"error": message or f"Session search failed (HTTP {response.status_code})"}
        )
    sessions = body.get("data", []) if isinstance(body, dict) else []
    return json.dumps(
        {
            "sessions": sessions,
            "message": None
            if sessions
            else "No matching sessions this session's participants can read.",
        }
    )
