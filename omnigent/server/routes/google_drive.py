"""Per-session Google Drive grants and the Drive operations agents run through them."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import replace
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from omnigent.db.group_authority import group_name
from omnigent.server.auth import (
    LEVEL_EDIT,
    LEVEL_MANAGE,
    LEVEL_READ,
    RESERVED_USER_PUBLIC,
    AuthProvider,
)
from omnigent.server.google_drive import (
    DOCUMENT,
    FOLDER,
    SPREADSHEET,
    GoogleDriveClient,
    GoogleDriveError,
    resolve_google_drive_token,
)
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.server.routes.connections_google_drive import credential_user
from omnigent.stores import ConversationStore
from omnigent.stores.google_drive_grant_store import GoogleDriveGrant, GoogleDriveGrantStore
from omnigent.stores.permission_store import PermissionStore

_logger = logging.getLogger(__name__)
_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_FILE_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
_FILE_URL = re.compile(r"/d/([A-Za-z0-9_-]{10,200})|[?&]id=([A-Za-z0-9_-]{10,200})")


def parse_file_id(value: str) -> str | None:
    """Accept a raw Drive file id or a Docs/Sheets/Slides/Drive link."""
    value = value.strip()
    if _FILE_ID.fullmatch(value):
        return value
    match = _FILE_URL.search(value)
    return (match.group(1) or match.group(2)) if match else None


def principal_label(principal: str) -> str:
    if principal == RESERVED_USER_PUBLIC:
        return "Anyone with the link"
    name = group_name(principal)
    return f"Group: {name}" if name is not None else principal


def covers(acknowledged: dict[str, int], current: dict[str, int]) -> bool:
    return all(acknowledged.get(principal, 0) >= level for principal, level in current.items())


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["file", "drive"]
    file: str | None = Field(default=None, max_length=2048)
    access: Literal["read", "write"]
    generation: str = Field(min_length=1, max_length=64)
    audience: dict[str, int] = Field(default_factory=dict)


class AudienceConfirmation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audience: dict[str, int]


class CreateFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=256)
    kind: Literal["document", "spreadsheet"]
    content: str = Field(default="", max_length=1_000_000)
    account: str | None = Field(default=None, max_length=320)


class AppendText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=200_000)


class UpdateValues(BaseModel):
    model_config = ConfigDict(extra="forbid")
    range: str = Field(min_length=1, max_length=256)
    values: list[list[str | int | float | bool | None]] = Field(max_length=10_000)


def create_google_drive_router(
    connections: Any,
    client: GoogleDriveClient,
    grants: GoogleDriveGrantStore,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth: AuthProvider | None,
) -> APIRouter:
    """Expose grant management to session participants and Drive operations to agents."""

    def private(response: Response) -> None:
        response.headers["Cache-Control"] = "private, no-store"

    router = APIRouter(dependencies=[Depends(private)])

    def root_of(session_id: str) -> str:
        if not _SESSION_ID.fullmatch(session_id):
            raise HTTPException(404, "Session not found")
        current = conversations.get_conversation(session_id)
        seen = set()
        while current is not None and current.parent_conversation_id is not None:
            if current.id in seen:
                break
            seen.add(current.id)
            current = conversations.get_conversation(current.parent_conversation_id)
        if current is None:
            raise HTTPException(404, "Session not found")
        return current.id

    async def authorize(request: Request, session_id: str, level: int) -> tuple[str, str]:
        user = require_user(request, auth) or "local"
        root = await asyncio.to_thread(root_of, session_id)
        await require_access(user, session_id, level, permissions, conversations)
        return user, root

    def audience(root: str, exclude: str) -> dict[str, int]:
        if permissions is None:
            return {}
        result: dict[str, int] = {}
        after = None
        while True:
            page, after = permissions.list_for_session(root, limit=1000, after_user_id=after)
            for grant in page:
                if grant.user_id != exclude:
                    result[grant.user_id] = max(result.get(grant.user_id, 0), grant.level)
            if after is None:
                return result

    def level_of(user: str, root: str) -> int:
        if permissions is None:
            return LEVEL_MANAGE + 1
        for level in (LEVEL_MANAGE + 1, LEVEL_MANAGE, LEVEL_EDIT, LEVEL_READ):
            if check_session_access(user, root, level, permissions, conversations):
                return level
        return 0

    def grantor_level(user: str, root: str, members: dict[str, int] | None = None) -> int:
        """Session level from the person's own invitation, which holds for any caller."""
        if permissions is None or permissions.is_admin(user):
            return LEVEL_MANAGE + 1
        if members is None:
            members = audience(root, "")
        return max(members.get(user, 0), members.get(RESERVED_USER_PUBLIC, 0))

    def describe(members: dict[str, int]) -> list[dict[str, Any]]:
        return [
            {
                "principal": principal,
                "label": principal_label(principal),
                "level": level,
                "can_use_agent": level >= LEVEL_EDIT,
            }
            for principal, level in sorted(members.items())
        ]

    def evaluate_all(root: str) -> tuple[list[dict[str, Any]], dict[str, int]]:
        """Return each grant's state, reading sharing and each owner's connection once."""
        members = audience(root, "")
        owners: dict[str, tuple[Any, int]] = {}
        evaluated = []
        for grant in grants.list(root):
            if grant.user_id not in owners:
                owners[grant.user_id] = (
                    connections.get(grant.user_id),
                    grantor_level(grant.user_id, root, members),
                )
            connection, owner_level = owners[grant.user_id]
            new = {
                principal: level
                for principal, level in members.items()
                if principal != grant.user_id and grant.audience.get(principal, 0) < level
            }
            if connection is None or connection.metadata.get("generation") != (
                grant.connection_generation
            ):
                state = "disconnected"
            elif owner_level < LEVEL_EDIT:
                state = "inactive"
            elif new:
                state = "suspended"
            else:
                state = "active"
            evaluated.append(
                {
                    "grant": grant,
                    "state": state,
                    "email": connection.metadata.get("email") if connection else None,
                    "new_audience": new,
                }
            )
        return evaluated, members

    def snapshot(root: str, viewer: str, credential: str | None) -> dict[str, Any]:
        level = level_of(viewer, root)
        evaluated, members = evaluate_all(root)
        connection = connections.get(credential) if credential else None
        result: dict[str, Any] = {
            "session_id": root,
            "viewer": {
                "connected": connection is not None,
                "email": connection.metadata.get("email") if connection else None,
                "generation": connection.metadata.get("generation") if connection else None,
                "can_grant": credential is not None
                and grantor_level(credential, root, members) >= LEVEL_EDIT,
                "can_manage": level >= LEVEL_MANAGE,
            },
            "grants": [],
        }
        if level >= LEVEL_EDIT:
            exclude = credential or viewer
            result["audience"] = describe({k: v for k, v in members.items() if k != exclude})
        for info in evaluated:
            grant = info["grant"]
            result["grants"].append(
                {
                    "id": grant.id,
                    "kind": grant.kind,
                    "file_id": grant.file_id,
                    "name": grant.name,
                    "mime_type": grant.mime_type,
                    "access": grant.access,
                    "account": info["email"],
                    "mine": grant.user_id in (viewer, credential),
                    "state": info["state"],
                    "new_audience": describe(info["new_audience"]) if level >= LEVEL_EDIT else [],
                }
            )
        return result

    def optional_credential(request: Request) -> str | None:
        try:
            return credential_user(request, auth)
        except HTTPException:
            return None

    @router.get("/sessions/{session_id}/google-drive")
    async def get_grants(session_id: str, request: Request) -> dict[str, Any]:
        """Show every Drive grant on the session to anyone who can read it."""
        viewer, root = await authorize(request, session_id, LEVEL_READ)
        return await asyncio.to_thread(snapshot, root, viewer, optional_credential(request))

    @router.post(
        "/sessions/{session_id}/google-drive/grants",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def add_grant(session_id: str, body: GrantRequest, request: Request) -> dict[str, Any]:
        """Grant the session's agents one of the caller's files or their whole Drive."""
        user = credential_user(request, auth)
        _, root = await authorize(request, session_id, LEVEL_EDIT)
        if await asyncio.to_thread(grantor_level, user, root) < LEVEL_EDIT:
            raise HTTPException(
                403,
                "Sharing Google Drive needs edit access from your own invitation to this "
                "session; access through a group is not enough",
            )
        connection = await asyncio.to_thread(connections.get, user)
        if connection is None or connection.metadata.get("generation") != body.generation:
            raise HTTPException(409, "Google Drive connection changed; refresh and retry")
        current = await asyncio.to_thread(audience, root, user)
        if not covers(body.audience, current):
            raise HTTPException(
                409,
                {
                    "message": "Confirm who will be able to use this access",
                    "audience": describe(current),
                },
            )
        file_id = name = mime = None
        if body.kind == "file":
            file_id = parse_file_id(body.file or "")
            if file_id is None:
                raise HTTPException(422, "Enter a Google Drive file link or id")
            try:
                token = await resolve_google_drive_token(user, store=connections, client=client)
                if token is None:
                    raise HTTPException(409, "Connect Google Drive first")
                meta = await client.metadata(token["token"], file_id)
            except GoogleDriveError as exc:
                raise HTTPException(exc.status, str(exc)) from None
            if meta["mime_type"] == FOLDER:
                raise HTTPException(422, "Folders cannot be granted; choose individual files")
            if body.access == "write" and not meta["can_edit"]:
                raise HTTPException(422, "You can only view this file; grant read access instead")
            name, mime = meta["name"], meta["mime_type"]
        elif body.file:
            raise HTTPException(422, "A whole-Drive grant does not take a file")
        grant = GoogleDriveGrant(
            id=uuid.uuid4().hex,
            session_id=root,
            user_id=user,
            connection_generation=body.generation,
            kind=body.kind,
            access=body.access,
            created_at=int(time.time()),
            audience=current,
            file_id=file_id,
            name=name,
            mime_type=mime,
        )
        await asyncio.to_thread(grants.put, grant)
        _logger.info(
            "Google Drive %s grant %s added to session %s by %s (%s)",
            body.kind,
            grant.id,
            root,
            user,
            body.access,
        )
        return await asyncio.to_thread(snapshot, root, user, user)

    @router.post(
        "/sessions/{session_id}/google-drive/grants/{grant_id}/confirm",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def confirm_grant(
        session_id: str, grant_id: str, body: AudienceConfirmation, request: Request
    ) -> dict[str, Any]:
        """Resume a grant after its owner acknowledges the session's current sharing."""
        user = credential_user(request, auth)
        _, root = await authorize(request, session_id, LEVEL_EDIT)
        owned = await asyncio.to_thread(grants.list, root)
        if not any(g.id == grant_id and g.user_id == user for g in owned):
            raise HTTPException(404, "Grant not found")
        current = await asyncio.to_thread(audience, root, user)
        if not covers(body.audience, current):
            raise HTTPException(
                409,
                {
                    "message": "Sharing changed again; review who can use this access",
                    "audience": describe(current),
                },
            )
        if await asyncio.to_thread(grants.acknowledge, root, grant_id, user, current) is None:
            raise HTTPException(404, "Grant not found")
        _logger.info("Google Drive grant %s reconfirmed on session %s by %s", grant_id, root, user)
        return await asyncio.to_thread(snapshot, root, user, user)

    @router.delete(
        "/sessions/{session_id}/google-drive/grants/{grant_id}",
        dependencies=[Depends(require_trusted_origin)],
    )
    async def revoke_grant(session_id: str, grant_id: str, request: Request) -> dict[str, Any]:
        """Let the grant's owner or a session manager remove it."""
        user, root = await authorize(request, session_id, LEVEL_READ)
        credential = optional_credential(request)
        grant = next(
            (g for g in await asyncio.to_thread(grants.list, root) if g.id == grant_id), None
        )
        if grant is None:
            raise HTTPException(404, "Grant not found")
        if grant.user_id not in (user, credential) and (
            await asyncio.to_thread(level_of, user, root) < LEVEL_MANAGE
        ):
            raise HTTPException(403, "Only the grant's owner or a session manager can revoke it")
        await asyncio.to_thread(grants.delete, root, grant_id)
        _logger.info("Google Drive grant %s revoked on session %s by %s", grant_id, root, user)
        return await asyncio.to_thread(snapshot, root, user, credential)

    async def active(root: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        evaluated, _ = await asyncio.to_thread(evaluate_all, root)
        return (
            [info for info in evaluated if info["state"] == "active"],
            [info for info in evaluated if info["state"] != "active"],
        )

    async def token_for(info: dict[str, Any]) -> str | None:
        try:
            token = await resolve_google_drive_token(
                info["grant"].user_id, store=connections, client=client
            )
        except GoogleDriveError:
            return None
        if token is None or token["generation"] != info["grant"].connection_generation:
            return None
        return token["token"]

    def unavailable(inactive: list[dict[str, Any]], *, write: bool) -> HTTPException:
        paused = sorted({info["email"] or "a participant" for info in inactive})
        detail = (
            "This file is not available to this session"
            + (" for editing" if write else "")
            + ". Ask a session participant to add it with the Google Drive control in the "
            "session header"
            + (" with edit access" if write else "")
            + ", or to allow Drive access for this session."
        )
        if paused:
            detail += (
                " Drive access from "
                + ", ".join(paused)
                + " is paused until they reconnect, or re-confirm it after the session's "
                "sharing changed."
            )
        return HTTPException(403, detail)

    async def resolve(
        root: str, file_id: str, *, write: bool
    ) -> tuple[dict[str, Any], str, dict[str, Any]]:
        if not _FILE_ID.fullmatch(file_id):
            raise HTTPException(422, "Invalid Google Drive file id")
        usable, inactive = await active(root)
        ordered = [
            info
            for info in usable
            if info["grant"].kind == "file" and info["grant"].file_id == file_id
        ] + [info for info in usable if info["grant"].kind == "drive"]
        for info in ordered:
            if write and info["grant"].access != "write":
                continue
            token = await token_for(info)
            if token is None:
                continue
            try:
                meta = await client.metadata(token, file_id)
            except GoogleDriveError as exc:
                if exc.status == 404:
                    continue
                raise HTTPException(exc.status, str(exc)) from None
            if meta["mime_type"] == FOLDER:
                raise HTTPException(422, "Folders cannot be read; list or search for files")
            if write and not meta["can_edit"]:
                continue
            return info, token, meta
        raise unavailable(inactive, write=write)

    def audit(root: str, info: dict[str, Any], operation: str, file_id: str | None) -> None:
        _logger.info(
            "Google Drive %s on session %s via grant %s of %s: %s",
            operation,
            root,
            info["grant"].id,
            info["grant"].user_id,
            file_id,
        )

    def file_view(meta: dict[str, Any], info: dict[str, Any]) -> dict[str, Any]:
        return {**meta, "account": info["email"], "access": info["grant"].access}

    @router.get("/sessions/{session_id}/google-drive/agent/files")
    async def agent_list(
        session_id: str,
        request: Request,
        query: str = Query(default="", max_length=200),
        limit: int = Query(default=20, ge=1, le=50),
    ) -> dict[str, Any]:
        """List the files granted to the session and search Drives granted in full."""
        _, root = await authorize(request, session_id, LEVEL_EDIT)
        usable, inactive = await active(root)
        files = [
            {
                "id": info["grant"].file_id,
                "name": info["grant"].name,
                "mime_type": info["grant"].mime_type,
                "account": info["email"],
                "access": info["grant"].access,
            }
            for info in usable
            if info["grant"].kind == "file"
            and (not query or query.lower() in (info["grant"].name or "").lower())
        ]
        drives = [info for info in usable if info["grant"].kind == "drive"]
        results = []
        for info in drives:
            token = await token_for(info)
            if token is None:
                continue
            try:
                found = await client.search(token, query, limit)
            except GoogleDriveError as exc:
                raise HTTPException(exc.status, str(exc)) from None
            audit(root, info, "search", None)
            results.extend(file_view(meta, info) for meta in found)
        result: dict[str, Any] = {
            "granted_files": files,
            "drive_access": [
                {"account": info["email"], "access": info["grant"].access} for info in drives
            ],
            "search_results": results[:limit],
        }
        if inactive:
            result["paused_grants"] = len(inactive)
        if not usable:
            result["hint"] = (
                "No Google Drive access is active for this session. Ask a participant to add "
                "files or allow Drive access with the Google Drive control in the session header."
            )
        return result

    @router.get("/sessions/{session_id}/google-drive/agent/files/{file_id}")
    async def agent_read(
        session_id: str,
        file_id: str,
        request: Request,
        sheet_range: str | None = Query(default=None, alias="range", max_length=256),
    ) -> dict[str, Any]:
        """Read a granted file as text, or spreadsheet values."""
        _, root = await authorize(request, session_id, LEVEL_EDIT)
        info, token, meta = await resolve(root, file_id, write=False)
        try:
            content = await client.read(token, meta, sheet_range)
        except GoogleDriveError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        audit(root, info, "read", file_id)
        return {"file": file_view(meta, info), **content}

    @router.post("/sessions/{session_id}/google-drive/agent/files")
    async def agent_create(session_id: str, body: CreateFile, request: Request) -> dict[str, Any]:
        """Create a Doc or Sheet in a Drive granted in full with edit access."""
        _, root = await authorize(request, session_id, LEVEL_EDIT)
        usable, inactive = await active(root)
        writable = [
            info
            for info in usable
            if info["grant"].kind == "drive"
            and info["grant"].access == "write"
            and (body.account is None or (info["email"] or "").lower() == body.account.lower())
        ]
        if not writable:
            raise HTTPException(
                403,
                "Creating files needs a participant to allow Drive access with editing for "
                "this session.",
            )
        accounts = sorted({(info["email"] or "").lower() for info in writable})
        if len(accounts) > 1:
            raise HTTPException(
                409, "Several Drives allow editing; pass account as one of: " + ", ".join(accounts)
            )
        info = writable[0]
        token = await token_for(info)
        if token is None:
            raise unavailable(inactive, write=True)
        try:
            meta = await client.create(token, body.title, body.kind, body.content)
        except GoogleDriveError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        source = info["grant"]
        derived = await asyncio.to_thread(
            grants.derive,
            source,
            replace(
                source,
                id=uuid.uuid4().hex,
                kind="file",
                file_id=meta["id"],
                name=meta["name"],
                mime_type=meta["mime_type"],
                created_at=int(time.time()),
            ),
        )
        audit(root, info, "create", meta["id"])
        result: dict[str, Any] = {"file": file_view(meta, info)}
        if derived is None:
            result["warning"] = (
                "Drive access changed while the file was being created, so it was not added "
                "to this session"
            )
        return result

    @router.post("/sessions/{session_id}/google-drive/agent/files/{file_id}/append")
    async def agent_append(
        session_id: str, file_id: str, body: AppendText, request: Request
    ) -> dict[str, Any]:
        """Append text to the end of a granted Google Doc."""
        _, root = await authorize(request, session_id, LEVEL_EDIT)
        info, token, meta = await resolve(root, file_id, write=True)
        if meta["mime_type"] != DOCUMENT:
            raise HTTPException(415, "Text can only be appended to a Google Doc")
        try:
            await client.append(token, file_id, body.text)
        except GoogleDriveError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        audit(root, info, "append", file_id)
        return {"file": file_view(meta, info), "appended_characters": len(body.text)}

    @router.put("/sessions/{session_id}/google-drive/agent/files/{file_id}/values")
    async def agent_update_values(
        session_id: str, file_id: str, body: UpdateValues, request: Request
    ) -> dict[str, Any]:
        """Write cell values into a granted Google Sheet."""
        _, root = await authorize(request, session_id, LEVEL_EDIT)
        if any(len(row) > 1000 for row in body.values):
            raise HTTPException(422, "Rows can have at most 1000 cells")
        info, token, meta = await resolve(root, file_id, write=True)
        if meta["mime_type"] != SPREADSHEET:
            raise HTTPException(415, "Cell values can only be written to a Google Sheet")
        try:
            result = await client.update_values(token, file_id, body.range, body.values)
        except GoogleDriveError as exc:
            raise HTTPException(exc.status, str(exc)) from None
        audit(root, info, "update_values", file_id)
        return {"file": file_view(meta, info), **result}

    return router
