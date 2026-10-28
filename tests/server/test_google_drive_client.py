"""Drive, Docs and Sheets request shapes and error mapping."""

from __future__ import annotations

import json

import httpx
import pytest

from omnigent.server import google_drive
from omnigent.server.google_drive import (
    DOCUMENT,
    SPREADSHEET,
    GoogleDriveClient,
    GoogleDriveConfig,
    GoogleDriveError,
)


@pytest.fixture
def drive():
    requests: list[httpx.Request] = []
    replies: dict[str, httpx.Response] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        for prefix, reply in replies.items():
            if f"{request.url.host}{request.url.path}".startswith(prefix):
                return reply
        return httpx.Response(404, json={"error": {"message": "missing"}})

    client = GoogleDriveClient(
        GoogleDriveConfig("id", "secret", "https://app/cb"), httpx.MockTransport(handler)
    )
    return client, requests, replies


async def test_document_export_is_markdown(drive):
    client, requests, replies = drive
    replies["www.googleapis.com/drive/v3/files/doc1234567/export"] = httpx.Response(
        200, content=b"# Title"
    )
    result = await client.read("tok", {"id": "doc1234567", "mime_type": DOCUMENT}, None)
    assert result == {"format": "text/markdown", "content": "# Title", "truncated": False}
    assert requests[0].url.params["mimeType"] == "text/markdown"
    assert requests[0].headers["Authorization"] == "Bearer tok"


async def test_sheet_reads_first_sheet_by_default(drive):
    client, requests, replies = drive
    base = "sheets.googleapis.com/v4/spreadsheets/sheet123456"
    replies[f"{base}/values"] = httpx.Response(
        200, json={"range": "'It''s'!A1:B2", "values": [["a", "b"]]}
    )
    replies[base] = httpx.Response(
        200,
        json={
            "sheets": [
                {"properties": {"title": "It's", "gridProperties": {"rowCount": 9}}},
                {"properties": {"title": "Other"}},
            ]
        },
    )
    result = await client.read("tok", {"id": "sheet123456", "mime_type": SPREADSHEET}, None)
    assert result["values"] == [["a", "b"]]
    assert [s["title"] for s in result["sheets"]] == ["It's", "Other"]
    assert requests[1].url.path.endswith("/values/'It''s'")


async def test_errors_never_leak_and_map_status(drive):
    client, _, replies = drive
    with pytest.raises(GoogleDriveError) as missing:
        await client.metadata("tok", "nofile12345")
    assert missing.value.status == 404
    replies["www.googleapis.com/drive/v3/files/limited1234"] = httpx.Response(429)
    with pytest.raises(GoogleDriveError) as limited:
        await client.metadata("tok", "limited1234")
    assert limited.value.status == 429
    assert "tok" not in str(limited.value)


async def test_reads_are_size_limited(drive, monkeypatch):
    client, _, replies = drive
    monkeypatch.setattr(google_drive, "MAX_DOWNLOAD_BYTES", 4)
    replies["www.googleapis.com/drive/v3/files/big1234567"] = httpx.Response(200, content=b"12345")
    with pytest.raises(GoogleDriveError) as error:
        await client.read("tok", {"id": "big1234567", "mime_type": "text/plain"}, None)
    assert error.value.status == 413


async def test_binary_and_unsupported_files_are_rejected(drive):
    client, _, _ = drive
    for mime in ("application/pdf", "application/vnd.google-apps.form"):
        with pytest.raises(GoogleDriveError) as error:
            await client.read("tok", {"id": "file1234567", "mime_type": mime}, None)
        assert error.value.status == 415


async def test_search_escapes_query(drive):
    client, requests, replies = drive
    replies["www.googleapis.com/drive/v3/files"] = httpx.Response(200, json={"files": []})
    await client.search("tok", "it's \\ here", 5)
    query = requests[0].url.params["q"]
    assert "name contains 'it\\'s \\\\ here'" in query
    assert "trashed = false" in query


async def test_create_converts_markdown_to_doc(drive):
    client, requests, replies = drive
    replies["www.googleapis.com/upload/drive/v3/files"] = httpx.Response(
        200, json={"id": "new1234567", "name": "Notes", "mimeType": DOCUMENT}
    )
    result = await client.create("tok", "Notes", "document", "# Hi")
    assert result["id"] == "new1234567"
    body = requests[0].content.decode()
    assert json.dumps({"name": "Notes", "mimeType": DOCUMENT}) in body
    assert "Content-Type: text/markdown" in body
