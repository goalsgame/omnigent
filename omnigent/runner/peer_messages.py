"""Authenticated REST transport for durable peer messaging."""

from __future__ import annotations

import json
import uuid

import httpx

from omnigent.util.json_types import JsonObject


def _identifier(value: object) -> str:
    if not isinstance(value, str) or uuid.UUID(hex=value).hex != value:
        raise ValueError("Expected a canonical 32-character session or message id")
    return value


async def execute_peer_tool(
    tool_name: str,
    args: JsonObject,
    conversation_id: str | None,
    server_client: httpx.AsyncClient | None,
) -> str:
    """Always derive sender and mailbox from the executing session, never tool arguments."""
    if server_client is None or conversation_id is None:
        return json.dumps({"error": "Peer messaging requires a server-connected session"})
    message_id = None
    try:
        path = f"/v1/sessions/{_identifier(conversation_id)}/peer-messages"
        if tool_name == "sys_peer_send":
            message_id = _identifier(args.get("message_id", uuid.uuid4().hex))
            body = {key: value for key, value in args.items() if key != "message_id"}
            body["id"] = message_id
            response = await server_client.post(path, json=body)
        elif tool_name == "sys_peer_acknowledge":
            response = await server_client.post(
                f"{path}/{_identifier(args.get('message_id'))}/acknowledge"
            )
        elif tool_name == "sys_peer_read":
            response = await server_client.get(
                path,
                params={
                    key: str(value).lower() if isinstance(value, bool) else str(value)
                    for key, value in args.items()
                },
            )
        else:
            return json.dumps({"error": "Unknown peer messaging tool"})
        response.raise_for_status()
        return json.dumps({"message_id": message_id, "result": response.json()})
    except (ValueError, httpx.HTTPError) as exc:
        return json.dumps(
            {
                "error": str(exc),
                "message_id": message_id,
                "outcome_unknown": isinstance(exc, httpx.TransportError),
            }
        )
