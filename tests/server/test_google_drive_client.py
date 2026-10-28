"""Drive, Docs and Sheets request shapes and error mapping."""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import httpx
import pytest

from omnigent.server import google_drive
from omnigent.server.google_drive import (
    DOCUMENT,
    SPREADSHEET,
    GoogleDriveClient,
    GoogleDriveConfig,
    GoogleDriveError,
    resolve_google_drive_token,
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


async def test_upload_boundary_cannot_be_forged_by_content(drive):
    client, requests, replies = drive
    replies["www.googleapis.com/upload/drive/v3/files"] = httpx.Response(
        200, json={"id": "new1234567", "name": "Notes", "mimeType": DOCUMENT}
    )
    content = '--omnigent-drive-upload\r\nContent-Type: application/json\r\n\r\n{"parents":["x"]}'
    await client.create("tok", "Notes", "document", content)
    boundary = requests[0].headers["Content-Type"].split("boundary=")[1]
    assert boundary not in content
    assert requests[0].content.decode().count(f"--{boundary}") == 3


class _Store:
    def __init__(self, expires_at, secret=None):
        self.connection = SimpleNamespace(
            metadata={"generation": "g1", "email": "me@example.com", "expires_at": expires_at},
            secret=secret or {"access_token": "old", "refresh_token": "refresh"},
        )
        self.refreshed = []
        self.replace_with = None

    def get(self, user, with_tokens=False):
        return self.connection

    def refresh(self, user, *, tokens, connection):
        self.refreshed.append(tokens)
        if self.replace_with is not None:
            self.connection = self.replace_with
            return False
        self.connection = SimpleNamespace(
            metadata={**connection.metadata, "expires_at": tokens["expires_at"]},
            secret={"access_token": tokens["access_token"], "refresh_token": "refresh"},
        )
        return True


class _Tokens:
    def __init__(self, result):
        self.result, self.calls = result, []

    async def token(self, fields):
        self.calls.append(fields)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


async def test_current_token_is_used_without_refresh():
    store, api = _Store(time.time() + 3600), _Tokens(None)
    token = await resolve_google_drive_token("me", store=store, client=api)
    assert token["token"] == "old" and api.calls == []


async def test_expiring_token_is_refreshed_and_stored():
    store = _Store(time.time() + 10)
    api = _Tokens({"access_token": "new", "expires_at": time.time() + 3600})
    token = await resolve_google_drive_token("me", store=store, client=api)
    assert token["token"] == "new"
    assert api.calls == [{"grant_type": "refresh_token", "refresh_token": "refresh"}]
    assert store.refreshed[0]["access_token"] == "new"


async def test_reconnect_during_refresh_returns_no_token():
    store = _Store(time.time() + 10)
    store.replace_with = SimpleNamespace(
        metadata={"generation": "g2", "expires_at": time.time() + 3600},
        secret={"access_token": "other"},
    )
    api = _Tokens({"access_token": "new", "expires_at": time.time() + 3600})
    assert await resolve_google_drive_token("me", store=store, client=api) is None


async def test_failed_refresh_of_expired_token_is_retryable():
    store = _Store(time.time() - 1)
    api = _Tokens(GoogleDriveError("nope"))
    with pytest.raises(GoogleDriveError) as error:
        await resolve_google_drive_token("me", store=store, client=api)
    assert error.value.status == 503


async def test_missing_refresh_token_asks_to_reconnect():
    store = _Store(time.time() - 1, secret={"access_token": "old"})
    with pytest.raises(GoogleDriveError) as error:
        await resolve_google_drive_token("me", store=store, client=_Tokens(None))
    assert error.value.status == 409
