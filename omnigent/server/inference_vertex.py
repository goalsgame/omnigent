"""Preserve Vertex thought signatures through OpenAI reasoning-metadata clients."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from copy import deepcopy
from typing import Any

import httpx

_FORMAT = "google-vertex-v1"
_MAX_EVENT_BYTES = 16 * 1024 * 1024


def restore_thought_signatures(payload: dict[str, Any]) -> dict[str, Any]:
    """Restore opaque replay metadata on its original assistant tool call."""
    result = deepcopy(payload)
    messages = result.get("messages")
    if not isinstance(messages, list):
        return result
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        details = message.get("reasoning_details")
        if not isinstance(details, list):
            continue
        remaining = []
        for detail in details:
            if not isinstance(detail, dict) or detail.get("format") != _FORMAT:
                remaining.append(detail)
                continue
            if detail.get("type") != "reasoning.encrypted" or not isinstance(
                detail.get("data"), str
            ):
                raise ValueError("Invalid Vertex thought-signature metadata")
            target = message
            if detail.get("id") is not None:
                calls = message.get("tool_calls", [])
                if not isinstance(calls, list):
                    raise ValueError("Invalid Vertex thought-signature tool calls")
                matches = [c for c in calls if isinstance(c, dict) and c.get("id") == detail["id"]]
                if len(matches) != 1:
                    raise ValueError("Vertex thought signature has no unique tool call")
                target = matches[0]
            extra = target.setdefault("extra_content", {})
            if not isinstance(extra, dict):
                raise ValueError("Invalid Vertex extra content")
            google = extra.setdefault("google", {})
            if not isinstance(google, dict):
                raise ValueError("Invalid Vertex Google metadata")
            if google.get("thought_signature", detail["data"]) != detail["data"]:
                raise ValueError("Conflicting Vertex thought signatures")
            google["thought_signature"] = detail["data"]
        if remaining:
            message["reasoning_details"] = remaining
        else:
            message.pop("reasoning_details", None)
    return result


class _ReplayMetadata:
    def __init__(self) -> None:
        self.calls: dict[tuple[int, int], dict[str, str]] = {}

    def annotate(self, body: dict[str, Any]) -> None:
        for choice in body.get("choices") or []:
            part = choice.get("delta", choice.get("message"))
            if not isinstance(part, dict):
                continue
            details = []
            signature = self._signature(part)
            if isinstance(signature, str) and signature:
                details.append(self._detail(signature, None))
            for index, call in enumerate(part.get("tool_calls") or []):
                key = (choice.get("index", 0), call.get("index", index))
                state = self.calls.setdefault(key, {})
                if call.get("id"):
                    state["id"] = call["id"]
                signature = self._signature(call)
                if isinstance(signature, str) and signature:
                    state["signature"] = signature
                if state.get("id") and state.get("signature"):
                    details.append(self._detail(state.pop("signature"), state["id"]))
            if details:
                part["reasoning_details"] = [*(part.get("reasoning_details") or []), *details]

    @staticmethod
    def _signature(part: dict[str, Any]) -> str | None:
        extra = part.get("extra_content")
        google = extra.get("google") if isinstance(extra, dict) else None
        signature = google.get("thought_signature") if isinstance(google, dict) else None
        return signature if isinstance(signature, str) else None

    @staticmethod
    def _detail(signature: str, call_id: str | None) -> dict[str, Any]:
        return {"type": "reasoning.encrypted", "format": _FORMAT, "id": call_id, "data": signature}


async def vertex_response_bytes(response: httpx.Response) -> AsyncIterator[bytes]:
    """Expose signatures as replayable reasoning details, retaining native fields."""
    metadata = _ReplayMetadata()
    if "text/event-stream" not in response.headers.get("content-type", ""):
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > _MAX_EVENT_BYTES:
                raise ValueError("Vertex response exceeds replay-metadata limit")
        body = json.loads(data)
        original = deepcopy(body)
        metadata.annotate(body)
        yield json.dumps(body).encode() if body != original else bytes(data)
        return

    def event(lines: list[str]) -> bytes:
        data = "\n".join(line[5:].removeprefix(" ") for line in lines if line.startswith("data:"))
        if not data or data.strip() == "[DONE]":
            return ("\n".join(lines) + "\n\n").encode()
        body = json.loads(data)
        original = deepcopy(body)
        metadata.annotate(body)
        if body == original:
            return ("\n".join(lines) + "\n\n").encode()
        other = [line for line in lines if not line.startswith("data:")]
        return ("\n".join([*other, "data: " + json.dumps(body)]) + "\n\n").encode()

    lines: list[str] = []
    size = 0
    async for line in response.aiter_lines():
        size += len(line.encode()) + 1
        if size > _MAX_EVENT_BYTES:
            raise ValueError("Vertex event exceeds replay-metadata limit")
        if line:
            lines.append(line)
        else:
            yield event(lines)
            lines = []
            size = 0
    if lines:
        yield event(lines)
