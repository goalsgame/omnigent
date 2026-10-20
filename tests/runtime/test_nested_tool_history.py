"""Nested audit executions must not become model calls on a cross-harness fork."""

from unittest.mock import AsyncMock

import httpx
import pytest

from omnigent.entities import ConversationItem, parse_item_data
from omnigent.llms._responses_to_chat import responses_input_to_chat_messages
from omnigent.runner.session_history import build_session_history
from omnigent.runtime.prompt import history_to_input_items


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["runner", "runtime"])
async def test_nested_calls_replay_only_enclosing_call_across_harnesses(boundary: str) -> None:
    raw = [
        {
            "type": "function_call",
            "agent": "Pi",
            "name": "codemode",
            "call_id": "outer",
            "arguments": "{}",
        }
    ]
    for index in range(2):
        call_id = f"outer/{index}"
        raw.extend(
            [
                {
                    "type": "function_call",
                    "agent": "Pi",
                    "name": "docs__lookup",
                    "call_id": call_id,
                    "arguments": "{}",
                    "parent_call_id": "outer",
                },
                {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": "document",
                    "parent_call_id": "outer",
                },
            ]
        )
    raw.append({"type": "function_call_output", "call_id": "outer", "output": "two documents"})
    if boundary == "runner":
        async with httpx.AsyncClient() as client:
            history = build_session_history(
                _background_tasks=set(),
                _last_server_item_id={},
                _persist_cancellation_items=AsyncMock(),
                _session_histories={},
                _session_spec_cache={},
                server_client=client,
            )
            converted = history.convert_raw_items_to_input(raw)
    else:
        stored = [
            ConversationItem(
                id=f"item-{index}",
                type=item["type"],
                status="completed",
                response_id="r",
                created_at=index,
                data=parse_item_data(item["type"], item),
            )
            for index, item in enumerate(raw)
        ]
        converted = history_to_input_items(stored)
    assert [item["call_id"] for item in converted] == ["outer", "outer"]
    messages = responses_input_to_chat_messages(converted, None)
    assert [message["role"] for message in messages] == ["assistant", "tool"]
    assert messages[0]["tool_calls"][0]["id"] == "outer"
    assert messages[1]["tool_call_id"] == "outer"
    assert len(raw) == 6, "Audit entries must remain stored"
