"""Google Drive OAuth configuration, user tokens and the server-side API client."""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from omnigent.server.google_cloud import GoogleCloudClient, GoogleCloudConfig, GoogleCloudError

SCOPES = "openid email https://www.googleapis.com/auth/drive"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive"
FOLDER = "application/vnd.google-apps.folder"
DOCUMENT = "application/vnd.google-apps.document"
SPREADSHEET = "application/vnd.google-apps.spreadsheet"
PRESENTATION = "application/vnd.google-apps.presentation"
MAX_DOWNLOAD_BYTES = 2 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
_FILE_FIELDS = "id,name,mimeType,size,webViewLink,modifiedTime,trashed,capabilities(canEdit)"
_DRIVE = "https://www.googleapis.com/drive/v3"
_TEXT_TYPES = (
    "text/",
    "application/json",
    "application/xml",
    "application/x-yaml",
    "application/yaml",
    "application/javascript",
)


class GoogleDriveError(Exception):
    """A Drive request failed; the message is safe to show and never contains tokens."""

    def __init__(self, message: str, status: int = 502) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class GoogleDriveConfig:
    client_id: str
    client_secret: str
    redirect_uri: str

    @classmethod
    def from_env(cls) -> GoogleDriveConfig | None:
        if os.environ.get("OMNIGENT_GOOGLE_DRIVE_AUTH") != "1":
            return None
        values = [
            os.environ.get(f"OMNIGENT_GOOGLE_DRIVE_{key}", "").strip()
            for key in ("CLIENT_ID", "CLIENT_SECRET", "REDIRECT_URI")
        ]
        if not any(values):
            return None
        if not all(values):
            raise RuntimeError(
                "Google Drive OAuth requires CLIENT_ID, CLIENT_SECRET and REDIRECT_URI"
            )
        uri = urlsplit(values[2])
        if uri.scheme != "https" or not uri.netloc or uri.username or uri.fragment:
            raise RuntimeError("Google Drive OAuth redirect must be an HTTPS URL")
        return cls(*values)


def build_google_drive_connection(
    database_url: str,
) -> tuple[GoogleDriveConfig | None, Any | None]:
    """Return the configured Drive connection and its encrypted store, or ``(None, None)``."""
    config = GoogleDriveConfig.from_env()
    if config is None:
        return None, None
    from omnigent.connections.google_drive import GoogleDriveConnectionStore
    from omnigent.stores.credential_store import build_secret_cipher

    cipher = build_secret_cipher()
    if cipher is None:
        raise RuntimeError("Google Drive connections require credential encryption")
    return config, GoogleDriveConnectionStore(database_url, cipher)


def _drive_query_literal(value: str) -> str:
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _file(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": data.get("id"),
        "name": data.get("name"),
        "mime_type": data.get("mimeType"),
        "web_view_link": data.get("webViewLink"),
        "modified_time": data.get("modifiedTime"),
        "can_edit": bool((data.get("capabilities") or {}).get("canEdit")),
    }


def _truncate(text: str) -> dict[str, Any]:
    if len(text) <= MAX_TEXT_CHARS:
        return {"content": text, "truncated": False}
    return {"content": text[:MAX_TEXT_CHARS], "truncated": True}


@dataclass(frozen=True)
class _Reply:
    status: int
    content: bytes

    def json(self) -> Any:
        return json.loads(self.content)


class GoogleDriveClient(GoogleCloudClient):
    """OAuth token exchange plus the Drive, Docs and Sheets calls agents may make."""

    def __init__(
        self, config: GoogleDriveConfig, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        super().__init__(
            GoogleCloudConfig(config.client_id, config.client_secret, config.redirect_uri)
        )
        self._transport = transport

    async def token(self, fields: dict[str, str]) -> dict[str, Any]:
        try:
            return await super().token(fields)
        except GoogleCloudError:
            raise GoogleDriveError(
                "Google Drive authorization failed; reconnect your account"
            ) from None

    async def identity(self, access_token: str) -> dict[str, str]:
        try:
            return await super().identity(access_token)
        except GoogleCloudError:
            raise GoogleDriveError("Could not verify Google account") from None

    async def _request(
        self,
        method: str,
        url: str,
        token: str,
        *,
        params: dict[str, str] | None = None,
        json_body: Any = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
        limit: int | None = None,
    ) -> _Reply:
        body = bytearray()
        try:
            async with (
                httpx.AsyncClient(
                    timeout=30, trust_env=False, transport=self._transport
                ) as client,
                client.stream(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    content=content,
                    headers={"Authorization": f"Bearer {token}", **(headers or {})},
                ) as response,
            ):
                status = response.status_code
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if limit is not None and len(body) > limit:
                        raise GoogleDriveError(
                            "File is too large to read; read a smaller range", 413
                        )
        except httpx.HTTPError:
            raise GoogleDriveError("Google Drive is temporarily unavailable; retry") from None
        reply = _Reply(status, bytes(body))
        if status in (401, 403, 404):
            raise GoogleDriveError(
                "File not found or not accessible with the granted Google account", 404
            )
        if status == 429:
            raise GoogleDriveError("Google Drive rate limit reached; retry shortly", 429)
        if status >= 400:
            message = "Google Drive rejected the request"
            try:
                detail = reply.json().get("error", {}).get("message")
            except (ValueError, AttributeError):
                detail = None
            if isinstance(detail, str) and detail:
                message = f"{message}: {detail[:300]}"
            raise GoogleDriveError(message, 400 if status < 500 else 502)
        return reply

    async def metadata(self, token: str, file_id: str) -> dict[str, Any]:
        response = await self._request(
            "GET",
            f"{_DRIVE}/files/{quote(file_id, safe='')}",
            token,
            params={"fields": _FILE_FIELDS, "supportsAllDrives": "true"},
        )
        data = response.json()
        if data.get("trashed"):
            raise GoogleDriveError("File is in the trash", 404)
        return _file(data)

    async def search(self, token: str, query: str, limit: int = 20) -> list[dict[str, Any]]:
        clauses = ["trashed = false", f"mimeType != '{FOLDER}'"]
        if query.strip():
            literal = _drive_query_literal(query.strip())
            clauses.append(f"(name contains {literal} or fullText contains {literal})")
        params = {
            "q": " and ".join(clauses),
            "fields": f"files({_FILE_FIELDS})",
            "pageSize": str(max(1, min(limit, 50))),
            "corpora": "allDrives",
            "includeItemsFromAllDrives": "true",
            "supportsAllDrives": "true",
        }
        if not query.strip():
            params["orderBy"] = "modifiedTime desc"
        response = await self._request("GET", f"{_DRIVE}/files", token, params=params)
        return [_file(item) for item in response.json().get("files", [])]

    async def read(
        self, token: str, file: dict[str, Any], sheet_range: str | None
    ) -> dict[str, Any]:
        file_id = quote(file["id"], safe="")
        mime = file.get("mime_type") or ""
        if mime == SPREADSHEET:
            return await self._read_sheet(token, file["id"], sheet_range)
        if sheet_range:
            raise GoogleDriveError("A range can only be read from a spreadsheet", 400)
        if mime in (DOCUMENT, PRESENTATION):
            export = "text/markdown" if mime == DOCUMENT else "text/plain"
            response = await self._request(
                "GET",
                f"{_DRIVE}/files/{file_id}/export",
                token,
                params={"mimeType": export},
                limit=MAX_DOWNLOAD_BYTES,
            )
            return {"format": export, **_truncate(response.content.decode("utf-8", "replace"))}
        if mime.startswith("application/vnd.google-apps."):
            raise GoogleDriveError(f"Reading {mime} files is not supported", 415)
        if not mime.startswith(_TEXT_TYPES):
            raise GoogleDriveError(f"Reading binary {mime or 'files'} is not supported", 415)
        response = await self._request(
            "GET",
            f"{_DRIVE}/files/{file_id}",
            token,
            params={"alt": "media", "supportsAllDrives": "true"},
            limit=MAX_DOWNLOAD_BYTES,
        )
        return {"format": mime, **_truncate(response.content.decode("utf-8", "replace"))}

    async def _read_sheet(
        self, token: str, file_id: str, sheet_range: str | None
    ) -> dict[str, Any]:
        base = f"https://sheets.googleapis.com/v4/spreadsheets/{quote(file_id, safe='')}"
        info = await self._request(
            "GET", base, token, params={"fields": "sheets.properties(title,gridProperties)"}
        )
        sheets = [
            {
                "title": sheet["properties"]["title"],
                "rows": sheet["properties"].get("gridProperties", {}).get("rowCount"),
                "columns": sheet["properties"].get("gridProperties", {}).get("columnCount"),
            }
            for sheet in info.json().get("sheets", [])
        ]
        target = sheet_range or (
            "'" + sheets[0]["title"].replace("'", "''") + "'" if sheets else None
        )
        if target is None:
            return {"format": "sheets", "sheets": sheets, "values": []}
        values = await self._request(
            "GET",
            f"{base}/values/{quote(target, safe='')}",
            token,
            params={"valueRenderOption": "FORMATTED_VALUE"},
            limit=MAX_DOWNLOAD_BYTES,
        )
        data = values.json()
        rows = data.get("values", [])
        text = json.dumps(rows)
        truncated = len(text) > MAX_TEXT_CHARS
        while rows and len(json.dumps(rows)) > MAX_TEXT_CHARS:
            rows = rows[: len(rows) // 2]
        return {
            "format": "sheets",
            "sheets": sheets,
            "range": data.get("range", target),
            "values": rows,
            "truncated": truncated,
        }

    async def create(self, token: str, title: str, kind: str, content: str) -> dict[str, Any]:
        target, source = (
            (DOCUMENT, "text/markdown") if kind == "document" else (SPREADSHEET, "text/csv")
        )
        boundary = "omnigent-drive-upload"
        body = (
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
            + json.dumps({"name": title, "mimeType": target})
            + f"\r\n--{boundary}\r\nContent-Type: {source}; charset=UTF-8\r\n\r\n"
            + content
            + f"\r\n--{boundary}--"
        ).encode()
        response = await self._request(
            "POST",
            "https://www.googleapis.com/upload/drive/v3/files",
            token,
            params={
                "uploadType": "multipart",
                "supportsAllDrives": "true",
                "fields": _FILE_FIELDS,
            },
            content=body,
            headers={"Content-Type": f"multipart/related; boundary={boundary}"},
        )
        return _file(response.json())

    async def append(self, token: str, file_id: str, text: str) -> None:
        await self._request(
            "POST",
            f"https://docs.googleapis.com/v1/documents/{quote(file_id, safe='')}:batchUpdate",
            token,
            json_body={"requests": [{"insertText": {"endOfSegmentLocation": {}, "text": text}}]},
        )

    async def update_values(
        self, token: str, file_id: str, sheet_range: str, values: list[list[Any]]
    ) -> dict[str, Any]:
        response = await self._request(
            "PUT",
            f"https://sheets.googleapis.com/v4/spreadsheets/{quote(file_id, safe='')}"
            f"/values/{quote(sheet_range, safe='')}",
            token,
            params={"valueInputOption": "USER_ENTERED"},
            json_body={"range": sheet_range, "values": values},
        )
        data = response.json()
        return {
            "updated_range": data.get("updatedRange"),
            "updated_cells": data.get("updatedCells"),
        }


async def resolve_google_drive_token(
    user_id: str, *, store: Any, client: GoogleDriveClient
) -> dict[str, Any] | None:
    """Return a fresh access token for *user_id*'s current Drive connection, or ``None``."""
    connection = await asyncio.to_thread(store.get, user_id, with_tokens=True)
    if connection is None:
        return None
    if connection.metadata.get("expires_at", 0) <= time.time() + 120:
        refresh = (connection.secret or {}).get("refresh_token")
        if not refresh:
            raise GoogleDriveError("Reconnect your Google Drive account", 409)
        try:
            tokens = await client.token({"grant_type": "refresh_token", "refresh_token": refresh})
        except GoogleDriveError:
            updated = False
        else:
            updated = await asyncio.to_thread(
                store.refresh, user_id, tokens=tokens, connection=connection
            )
        current = await asyncio.to_thread(store.get, user_id, with_tokens=True)
        if current is None or current.metadata.get("generation") != connection.metadata.get(
            "generation"
        ):
            return None
        if not updated and (
            current.metadata == connection.metadata
            or current.metadata.get("expires_at", 0) <= time.time() + 5
        ):
            raise GoogleDriveError("Google Drive refresh temporarily unavailable; retry", 503)
        connection = current
    return {
        "token": (connection.secret or {})["access_token"],
        "email": connection.metadata.get("email"),
        "generation": connection.metadata.get("generation"),
    }
