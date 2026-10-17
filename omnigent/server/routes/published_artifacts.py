"""Authenticated publishing, sharing and isolated viewing of durable artifacts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import uuid
from dataclasses import dataclass, replace
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, Field
from starlette.datastructures import Headers
from starlette.types import Message, Receive, Scope, Send

from omnigent.db.account_authority import account_generation
from omnigent.db.group_authority import GROUP_PRINCIPAL_PREFIX, access_principals, group_principal
from omnigent.db.utils import now_epoch
from omnigent.server.auth import AuthProvider, SharingMode
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores import ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.permission_store import PermissionStore
from omnigent.stores.published_artifact_store import PublishedArtifact, PublishedArtifactStore

_LOGGER = logging.getLogger(__name__)
_UUID = re.compile(r"[a-f0-9]{32}")
RENDERER_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
    "img-src data: blob:; font-src data:; media-src data: blob:; connect-src 'none'; "
    "frame-src 'none'; object-src 'none'; base-uri 'none'; form-action 'none'; "
    "sandbox allow-scripts"
)
ARTIFACT_RENDERER_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="referrer" content="no-referrer"><style>
html,body,iframe{margin:0;border:0;width:100%;height:100%;display:block}
</style></head><body><iframe title="Artifact content" sandbox="allow-scripts"
referrerpolicy="no-referrer"></iframe><script>
let initialized=false;
addEventListener('message',event=>{
  if(initialized || event.source!==parent ||
     event.data?.type!=='omnigent.artifact.render' || typeof event.data.html!=='string')return;
  initialized=true;
  document.querySelector('iframe').srcdoc='<!doctype html>'+event.data.html;
});
</script></body></html>"""


@dataclass(frozen=True)
class PublicationLimits:
    """Operator limits independent of managed sandbox retention."""

    max_bytes: int = 50 * 1024 * 1024
    max_html_bytes: int = 5 * 1024 * 1024
    retention_days: int = 90
    max_retention_days: int = 365
    public_url: str | None = None

    @classmethod
    def parse(cls, raw: Any) -> PublicationLimits:
        """Reject misspelled limits and invalid values at application startup."""
        if raw is None:
            return cls()
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise ValueError("published_artifacts must contain only supported limits")
        if any(
            type(value) is not int or value <= 0
            for key, value in raw.items()
            if key != "public_url"
        ):
            raise ValueError("published_artifacts limits must be positive integers")
        public_url = raw.get("public_url")
        if public_url is not None:
            from urllib.parse import urlsplit

            if (
                not isinstance(public_url, str)
                or not public_url.isascii()
                or any(ord(c) <= 32 or ord(c) == 127 for c in public_url)
            ):
                raise ValueError("published_artifacts.public_url must be an HTTPS origin")
            try:
                parsed = urlsplit(public_url)
                valid = parsed.scheme == "https" and parsed.hostname and parsed.port != 0
            except ValueError as exc:
                raise ValueError("Invalid published_artifacts.public_url") from exc
            if (
                not valid
                or parsed.username
                or parsed.password
                or parsed.path not in ("", "/")
                or parsed.query
                or parsed.fragment
                or "?" in public_url
                or "#" in public_url
                or "%" in public_url
            ):
                raise ValueError("published_artifacts.public_url must be an HTTPS origin")
        limits = cls(**raw)
        if (
            limits.retention_days > limits.max_retention_days
            or limits.max_html_bytes > limits.max_bytes
        ):
            raise ValueError("published_artifacts defaults cannot exceed maximum limits")
        return limits


class ArtifactGrant(BaseModel):
    """An exact user identity or verified OIDC group receiving read access."""

    principal_type: Literal["user", "group"]
    principal: str = Field(min_length=1, max_length=128)


class ArtifactSharing(BaseModel):
    """Replace the complete set of explicit read grants; ownership is immutable."""

    grants: list[ArtifactGrant] = Field(max_length=100)


def _owned(item: PublishedArtifact, actor: str) -> bool:
    return item.owner_id == actor and item.owner_generation == account_generation(actor)


def _readable(item: PublishedArtifact, actor: str) -> bool:
    principals = access_principals(actor)
    return _owned(item, actor) or any(
        (
            grant["principal"] in principals[1:]
            if grant["is_group"]
            else (
                grant["principal"] == actor
                and grant.get("account_generation") == account_generation(actor)
            )
        )
        for grant in json.loads(item.grants_json)
    )


def create_published_artifacts_router(
    store: PublishedArtifactStore,
    blobs: ArtifactStore,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth: AuthProvider | None,
    limits: PublicationLimits,
) -> APIRouter:
    """Build publication routes without coupling their reads to session existence."""

    def private_response(response: Response) -> None:
        response.headers["Cache-Control"] = "private, no-store"

    class BoundedPublicationRoute(APIRoute):
        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] != "http" or scope["method"] not in {"POST", "PUT"}:
                await super().handle(scope, receive, send)
                return
            budget = limits.max_bytes + 64 * 1024 if scope["method"] == "POST" else 64 * 1024
            declared = Headers(scope=scope).get("content-length")
            if declared is not None:
                try:
                    size = int(declared)
                except ValueError as exc:
                    raise HTTPException(400, "Invalid Content-Length") from exc
                if size < 0:
                    raise HTTPException(400, "Invalid Content-Length")
                if size > budget:
                    raise HTTPException(413, "Artifact request exceeds the upload limit")
            request = Request(scope)
            user = actor(request)
            require_trusted_origin(request)
            session_id = scope.get("path_params", {}).get("session_id")
            if scope["method"] == "POST" and session_id:
                if not _UUID.fullmatch(session_id):
                    raise HTTPException(404, "Session not found")
                await require_access(user, session_id, 2, permissions, conversations)
            received = 0

            async def bounded_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > budget:
                        raise HTTPException(413, "Artifact request exceeds the upload limit")
                return message

            await super().handle(scope, bounded_receive, send)

    router = APIRouter(
        route_class=BoundedPublicationRoute, dependencies=[Depends(private_response)]
    )

    @router.get("/artifact-renderer", include_in_schema=False)
    async def renderer() -> Response:
        # This public shell has no artifact bytes, credentials or application bridge.
        return Response(
            ARTIFACT_RENDERER_HTML,
            media_type="text/html",
            headers={
                "Content-Security-Policy": RENDERER_CSP,
                "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "public, max-age=3600",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            },
        )

    def actor(request: Request) -> str:
        return require_user(request, auth) or "local"

    async def authorized(
        request: Request, artifact_id: str, *, owner: bool = False
    ) -> PublishedArtifact:
        user = actor(request)
        if not _UUID.fullmatch(artifact_id):
            raise HTTPException(404, "Artifact not found")
        item = await asyncio.to_thread(store.get, artifact_id)
        if item is None or not (_owned(item, user) if owner else _readable(item, user)):
            raise HTTPException(404, "Artifact not found")
        return item

    def payload(item: PublishedArtifact, request: Request) -> dict[str, Any]:
        root = (
            request.scope.get("root_path") or getattr(request.app.state, "base_path", "") or ""
        ).rstrip("/")
        result = {
            key: getattr(item, key)
            for key in (
                "id",
                "owner_id",
                "source_session_id",
                "title",
                "filename",
                "kind",
                "content_type",
                "byte_size",
                "sha256",
                "created_at",
                "expires_at",
            )
        }
        origin = limits.public_url.rstrip("/") if limits.public_url else ""
        result.update(
            viewer_url=f"{origin}{root}/artifacts/{item.id}",
            download_url=f"{origin}{root}/v1/artifacts/{item.id}/content",
            renderer_url=f"{origin}{root}/v1/artifact-renderer",
            can_manage=_owned(item, actor(request)),
        )
        if _owned(item, actor(request)):
            from omnigent.db.group_authority import group_name

            result["grants"] = [
                {
                    "principal_type": "group" if grant["is_group"] else "user",
                    "principal": group_name(grant["principal"])
                    if grant["is_group"]
                    else grant["principal"],
                }
                for grant in json.loads(item.grants_json)
            ]
        return result

    @router.post(
        "/sessions/{session_id}/artifacts",
        status_code=201,
        dependencies=[Depends(require_trusted_origin)],
    )
    async def publish(
        request: Request,
        session_id: str,
        file: Annotated[UploadFile, File()],
        title: Annotated[str, Form(min_length=1, max_length=256)],
        kind: Annotated[Literal["file", "html"], Form()] = "file",
        retention_days: Annotated[int | None, Form(ge=1)] = None,
    ) -> dict[str, Any]:
        """Copy a file or self-contained UTF-8 HTML document into durable storage."""
        user = actor(request)
        if not _UUID.fullmatch(session_id):
            raise HTTPException(404, "Session not found")
        await require_access(user, session_id, 2, permissions, conversations)
        if await asyncio.to_thread(conversations.get_conversation, session_id) is None:
            raise HTTPException(404, "Session not found")
        days = limits.retention_days if retention_days is None else retention_days
        if days > limits.max_retention_days:
            raise HTTPException(422, "Retention exceeds the configured maximum")
        filename = (file.filename or "artifact").replace("\\", "/").rsplit("/", 1)[-1]
        if (
            not filename
            or len(filename) > 256
            or any(ord(c) < 32 or ord(c) == 127 for c in filename)
        ):
            raise HTTPException(422, "Invalid artifact filename")
        maximum = limits.max_html_bytes if kind == "html" else limits.max_bytes
        data = bytearray()
        while chunk := await file.read(min(1024 * 1024, maximum + 1 - len(data))):
            data.extend(chunk)
            if len(data) > maximum:
                raise HTTPException(413, "Artifact exceeds the configured size limit")
        content = bytes(data)
        if kind == "html":
            try:
                content.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise HTTPException(422, "HTML artifacts must be UTF-8") from exc
        item = PublishedArtifact(
            id=uuid.uuid4().hex,
            owner_id=user,
            owner_generation=account_generation(user),
            source_session_id=session_id,
            title=title,
            filename=filename,
            kind=kind,
            content_type="text/html" if kind == "html" else "application/octet-stream",
            byte_size=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            created_at=now_epoch(),
            expires_at=now_epoch() + days * 86400,
        )

        # Complete both writes in one worker even if the client disconnects.
        def persist() -> None:
            store.create(replace(item, ready=False))
            try:
                blobs.put(item.blob_key(), content)
                if not store.complete(item.id):
                    raise RuntimeError("Publication no longer pending")
            except BaseException:
                # A lost commit acknowledgement must not erase a committed publication.
                if store.get(item.id) is not None:
                    return
                store.mutate(item.id, user, delete=True)
                blobs.delete(item.blob_key())
                raise

        await asyncio.to_thread(persist)
        return payload(item, request)

    @router.get("/artifacts")
    async def list_artifacts(request: Request) -> dict[str, Any]:
        """List up to 100 publications owned by the authenticated caller."""
        items = await asyncio.to_thread(store.list_owned, actor(request))
        return {"data": [payload(item, request) for item in items]}

    @router.get("/artifacts/{artifact_id}")
    async def get_artifact(request: Request, artifact_id: str) -> dict[str, Any]:
        """Return metadata only to the owner or a currently authorized reader."""
        return payload(await authorized(request, artifact_id), request)

    @router.put(
        "/artifacts/{artifact_id}/permissions", dependencies=[Depends(require_trusted_origin)]
    )
    async def share(request: Request, artifact_id: str, body: ArtifactSharing) -> dict[str, Any]:
        """Replace read grants; groups match exact, verified issuer claims."""
        item = await authorized(request, artifact_id, owner=True)
        grants = []
        for grant in body.grants:
            try:
                principal = (
                    group_principal(grant.principal)
                    if grant.principal_type == "group"
                    else grant.principal
                )
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc
            if grant.principal_type == "user" and (
                principal.startswith((GROUP_PRINCIPAL_PREFIX, "__"))
                or principal != principal.strip()
                or any(ord(c) < 32 for c in principal)
            ):
                raise HTTPException(422, "Invalid user identity")
            entry: dict[str, Any] = {
                "principal": principal,
                "is_group": grant.principal_type == "group",
            }
            if grant.principal_type == "user":
                target = (
                    await asyncio.to_thread(permissions.get_user, principal)
                    if permissions
                    else None
                )
                entry["account_generation"] = target.account_generation if target else None
            if entry not in grants:
                grants.append(entry)
        sharing_mode = getattr(request.app.state, "sharing_mode", lambda: SharingMode.ON)()
        if sharing_mode == SharingMode.OFF and any(
            entry not in json.loads(item.grants_json) for entry in grants
        ):
            raise HTTPException(403, "Sharing has been disabled for this server")
        if not await asyncio.to_thread(store.mutate, item.id, actor(request), grants=grants):
            raise HTTPException(404, "Artifact not found")
        return payload(await authorized(request, artifact_id, owner=True), request)

    @router.delete(
        "/artifacts/{artifact_id}", status_code=204, dependencies=[Depends(require_trusted_origin)]
    )
    async def delete(request: Request, artifact_id: str) -> Response:
        """Revoke access immediately; the collector retries physical blob removal."""
        await authorized(request, artifact_id, owner=True)
        await asyncio.to_thread(store.mutate, artifact_id, actor(request), delete=True)
        return Response(status_code=204)

    async def content(request: Request, artifact_id: str, *, preview: bool) -> Response:
        item = await authorized(request, artifact_id)
        if preview and item.kind != "html":
            raise HTTPException(404, "HTML preview not available")
        try:
            data = await asyncio.to_thread(blobs.get, item.blob_key())
        except KeyError as exc:
            raise HTTPException(404, "Artifact content unavailable") from exc
        if hashlib.sha256(data).hexdigest() != item.sha256:
            raise HTTPException(503, "Artifact integrity check failed")
        from urllib.parse import quote

        headers = {
            "Cache-Control": "private, no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Disposition": "attachment; filename*=UTF-8''" + quote(item.filename, safe=""),
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
        }
        return Response(
            data,
            media_type="text/plain; charset=utf-8" if preview else "application/octet-stream",
            headers=headers,
        )

    @router.get("/artifacts/{artifact_id}/content")
    async def download(request: Request, artifact_id: str) -> Response:
        """Download verified bytes with attachment disposition for every file type."""
        return await content(request, artifact_id, preview=False)

    @router.get("/artifacts/{artifact_id}/preview")
    async def preview(request: Request, artifact_id: str) -> Response:
        """Return non-executable source for the isolated renderer; direct navigation downloads."""
        return await content(request, artifact_id, preview=True)

    return router


async def collect_published_artifacts(store: PublishedArtifactStore, blobs: ArtifactStore) -> None:
    """Retry expired/tombstoned blob deletion without retaining sandbox resources."""
    while True:
        try:
            after = None
            while rows := await asyncio.to_thread(store.garbage, after=after):
                after = (rows[-1][0], rows[-1][1].id)
                for workspace_id, item in rows:
                    try:
                        if not await asyncio.to_thread(store.claim_garbage, workspace_id, item.id):
                            continue
                        await asyncio.to_thread(blobs.delete, item.blob_key(workspace_id))
                        await asyncio.to_thread(store.forget, workspace_id, item.id)
                    except Exception:
                        _LOGGER.exception("Published artifact blob cleanup failed; retrying")
        except Exception:
            _LOGGER.exception("Published artifact cleanup failed; retrying next cycle")
        await asyncio.sleep(60)
