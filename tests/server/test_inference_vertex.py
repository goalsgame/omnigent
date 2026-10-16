"""Verify opaque Vertex signatures survive OpenAI tool-result replay."""

import json

import httpx
import pytest

from omnigent.server.inference_upstreams import InferenceUpstream
from omnigent.server.inference_vertex import restore_thought_signatures, vertex_response_bytes


class Chunks(httpx.AsyncByteStream):
    def __init__(self, body: bytes):
        self.body = body

    async def __aiter__(self):
        for start in range(0, len(self.body), 7):
            yield self.body[start : start + 7]


def tool(index, call_id=None, signature=None):
    value = {"index": index, "type": "function", "function": {"name": "echo", "arguments": "{}"}}
    if call_id:
        value["id"] = call_id
    if signature:
        value["extra_content"] = {"google": {"thought_signature": signature}}
    return value


def chunk(calls, choice=0):
    return {"choices": [{"index": choice, "delta": {"tool_calls": calls}}]}


@pytest.mark.asyncio
async def test_fragmented_parallel_tool_signatures_round_trip():
    frames = [
        chunk([tool(0, "call-a"), tool(1, signature="opaque-b")]),
        chunk([tool(0, signature="opaque-a"), tool(1, "call-b")]),
        chunk([tool(0, "call-c", "opaque-c")], choice=1),
    ]
    wire = ": keepalive\r\n\r\n" + "".join("data: " + json.dumps(f) + "\r\n\r\n" for f in frames)
    wire += "data: [DONE]\r\n\r\n"
    response = httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks(wire.encode())
    )
    output = b"".join([part async for part in vertex_response_bytes(response)]).decode()
    assert ": keepalive\n\n" in output and output.endswith("data: [DONE]\n\n")
    bodies = [json.loads(line[6:]) for line in output.splitlines() if line.startswith("data: {")]
    details = [
        d for b in bodies for c in b["choices"] for d in c["delta"].get("reasoning_details", [])
    ]
    assert {d["id"]: d["data"] for d in details} == {
        "call-a": "opaque-a",
        "call-b": "opaque-b",
        "call-c": "opaque-c",
    }
    replay = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [tool(0, "call-a"), tool(1, "call-b")],
                "reasoning_details": details[:2],
            }
        ]
    }
    restored = restore_thought_signatures(replay)["messages"][0]
    assert "reasoning_details" not in restored
    assert [c["extra_content"]["google"]["thought_signature"] for c in restored["tool_calls"]] == [
        "opaque-a",
        "opaque-b",
    ]
    assert "extra_content" not in replay["messages"][0]["tool_calls"][0]


@pytest.mark.asyncio
async def test_nonstream_message_and_tool_metadata_round_trip():
    message = {
        "role": "assistant",
        "content": "héllo",
        "tool_calls": [tool(0, "call", "tool-sig")],
        "extra_content": {"google": {"thought_signature": "message-sig"}},
    }
    body = {"choices": [{"index": 0, "message": message}]}
    response = httpx.Response(
        200, headers={"content-type": "application/json"}, stream=Chunks(json.dumps(body).encode())
    )
    output = json.loads(b"".join([part async for part in vertex_response_bytes(response)]))
    assistant = output["choices"][0]["message"]
    assert assistant["extra_content"] == message["extra_content"]
    replay = {"messages": [assistant]}
    assert restore_thought_signatures(replay) == {"messages": [message]}


@pytest.mark.asyncio
async def test_multiline_sse_and_unterminated_final_event():
    data = 'event: message\ndata: {"choices":\ndata: []}'
    response = httpx.Response(
        200, headers={"content-type": "text/event-stream"}, stream=Chunks(data.encode())
    )
    output = b"".join([part async for part in vertex_response_bytes(response)]).decode()
    assert output.startswith("event: message\n")
    data = "\n".join(
        line[5:].removeprefix(" ") for line in output.splitlines() if line.startswith("data:")
    )
    assert json.loads(data) == {"choices": []}


@pytest.mark.parametrize("ids", [[], ["other"], ["call", "call"]])
def test_orphan_or_ambiguous_signature_is_rejected(ids):
    payload = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [tool(i, key) for i, key in enumerate(ids)],
                "reasoning_details": [
                    {
                        "type": "reasoning.encrypted",
                        "format": "google-vertex-v1",
                        "id": "call",
                        "data": "signature",
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="unique tool call"):
        restore_thought_signatures(payload)


def test_native_signature_is_preserved_and_conflicts_are_rejected():
    payload = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [tool(0, "call", "native")],
                "reasoning_details": [
                    {
                        "type": "reasoning.encrypted",
                        "format": "google-vertex-v1",
                        "id": "call",
                        "data": "different",
                    }
                ],
            }
        ]
    }
    with pytest.raises(ValueError, match="Conflicting"):
        restore_thought_signatures(payload)


def test_other_upstreams_and_foreign_metadata_are_unchanged():
    payload = {
        "model": "alias",
        "messages": [
            {
                "role": "assistant",
                "reasoning_details": [
                    {"type": "reasoning.encrypted", "format": "other", "data": "opaque"}
                ],
            }
        ],
    }
    assert restore_thought_signatures(payload) == payload
    upstream = InferenceUpstream.parse({"kind": "openrouter"})
    _, body = upstream.request("chat/completions", payload, "target")
    assert body == {**payload, "model": "target"}


def test_vertex_request_restores_metadata_before_forwarding():
    upstream = InferenceUpstream.parse(
        {"kind": "vertex-openai", "project": "example-project", "location": "eu"}
    )
    payload = {
        "messages": [
            {
                "role": "assistant",
                "tool_calls": [tool(0, "call")],
                "reasoning_details": [
                    {
                        "type": "reasoning.encrypted",
                        "format": "google-vertex-v1",
                        "id": "call",
                        "data": "opaque",
                    }
                ],
            }
        ]
    }
    _, body = upstream.request("chat/completions", payload, "google/test-model")
    assert body["model"] == "google/test-model"
    assert (
        body["messages"][0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"]
        == "opaque"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [True, False])
@pytest.mark.parametrize(
    "metadata", [{}, {"extra_content": None}, {"extra_content": {"google": None}}]
)
async def test_plain_text_with_null_tool_calls_is_preserved(streaming, metadata):
    message = {"role": "assistant", "content": "hello", "tool_calls": None, **metadata}
    body = {"choices": [{"index": 0, "delta" if streaming else "message": message}]}
    wire = "data: " + json.dumps(body) + "\n\n" if streaming else json.dumps(body)
    response = httpx.Response(
        200,
        headers={"content-type": "text/event-stream" if streaming else "application/json"},
        stream=Chunks(wire.encode()),
    )
    output = b"".join([part async for part in vertex_response_bytes(response)])
    assert output == wire.encode()


@pytest.mark.asyncio
async def test_null_replay_details_accept_message_signature():
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "hello",
                    "tool_calls": None,
                    "reasoning_details": None,
                    "extra_content": {"google": {"thought_signature": "opaque"}},
                }
            }
        ]
    }
    response = httpx.Response(
        200, headers={"content-type": "application/json"}, stream=Chunks(json.dumps(body).encode())
    )
    output = json.loads(b"".join([part async for part in vertex_response_bytes(response)]))
    assert output["choices"][0]["message"]["reasoning_details"][0]["data"] == "opaque"
