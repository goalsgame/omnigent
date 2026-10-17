"""Publication authorization, independent lifetime and HTML isolation contracts."""

from __future__ import annotations

import asyncio
import hashlib
import json
import time

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import bind_group_authority, group_authority_scope
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.published_artifacts import (
    PublicationLimits,
    collect_published_artifacts,
    create_published_artifacts_router,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.stores.published_artifact_store import PublishedArtifact, PublishedArtifactStore


class _Auth(AuthProvider):
    def get_user_id(self, request):
        user = request.headers.get("x-test-user")
        if user:
            bind_group_authority(
                user, json.loads(request.headers.get("x-test-groups", "[]")), int(time.time()) + 60
            )
        return user


@pytest.fixture
def publication_env(db_uri, tmp_path):
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    store = PublishedArtifactStore(db_uri)
    blobs = LocalArtifactStore(str(tmp_path / "published"))
    session = conversations.create_conversation()
    for user in (
        "owner@example.test",
        "reader@example.test",
        "editor@example.test",
        "oidc-machine:bot",
    ):
        permissions.ensure_user(user)
    permissions.grant("owner@example.test", session.id, 4)
    permissions.grant("reader@example.test", session.id, 1)
    permissions.grant("editor@example.test", session.id, 2)
    permissions.grant("oidc-machine:bot", session.id, 2)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def error_handler(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=exc.http_status)

    @app.middleware("http")
    async def authority(request: Request, call_next):
        with (
            group_authority_scope(),
            workspace_scope(int(request.headers.get("x-test-workspace", "0"))),
        ):
            return await call_next(request)

    app.include_router(
        create_published_artifacts_router(
            store,
            blobs,
            conversations,
            permissions,
            _Auth(),
            PublicationLimits(max_bytes=1000, max_html_bytes=500),
        ),
        prefix="/v1",
    )
    return app, store, blobs, conversations, session.id


def _headers(user="owner@example.test", **extra):
    return {"x-test-user": user, **extra}


async def _publish(
    client,
    session_id,
    *,
    user="owner@example.test",
    kind="html",
    content=b"<h1>report</h1>",
    **data,
):
    return await client.post(
        f"/v1/sessions/{session_id}/artifacts",
        headers=_headers(user),
        data={"title": "Report", "kind": kind, **data},
        files={"file": ("report.html", content)},
    )


@pytest.mark.asyncio
async def test_publication_survives_source_deletion_and_remains_private(publication_env):
    app, store, blobs, conversations, session_id = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await _publish(client, session_id)
        assert response.status_code == 201, response.text
        item = response.json()
        path = f"/v1/artifacts/{item['id']}"
        assert item["viewer_url"] == f"/artifacts/{item['id']}"
        assert item["sha256"] == hashlib.sha256(b"<h1>report</h1>").hexdigest()
        assert (await client.get(path, headers=_headers("reader@example.test"))).status_code == 404
        await conversations.delete_conversation(session_id)
        assert conversations.get_conversation(session_id) is None
        assert (await client.get(path, headers=_headers())).status_code == 200
        assert (
            await client.get(path + "/content", headers=_headers())
        ).content == b"<h1>report</h1>"
        assert store.get(item["id"]) is not None
        assert blobs.exists(store.get(item["id"]).blob_key())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user,expected",
    [("reader@example.test", 403), ("editor@example.test", 201), ("oidc-machine:bot", 201)],
)
async def test_publish_requires_edit_and_uses_actual_identity(publication_env, user, expected):
    app, _, _, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        result = await _publish(client, sid, user=user)
        assert result.status_code == expected
        if expected == 201:
            assert result.json()["owner_id"] == user


@pytest.mark.asyncio
async def test_explicit_user_group_grants_revocation_and_tenant_isolation(publication_env):
    app, _, _, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        item = (await _publish(client, sid)).json()
        path = f"/v1/artifacts/{item['id']}"
        grants = [
            {"principal_type": "user", "principal": "reader@example.test"},
            {"principal_type": "group", "principal": "engineering"},
        ]
        result = await client.put(
            path + "/permissions", headers=_headers(), json={"grants": grants}
        )
        assert result.status_code == 200, result.text
        assert (await client.get(path, headers=_headers("reader@example.test"))).status_code == 200
        member = _headers("member@example.test", **{"x-test-groups": '["engineering"]'})
        assert (await client.get(path + "/preview", headers=member)).status_code == 200
        assert (await client.get(path, headers=_headers("member@example.test"))).status_code == 404
        assert (
            await client.get(path, headers={**member, "x-test-workspace": "42"})
        ).status_code == 404
        assert (
            await client.put(path + "/permissions", headers=member, json={"grants": []})
        ).status_code == 404
        assert (await client.delete(path, headers=member)).status_code == 404
        await client.put(path + "/permissions", headers=_headers(), json={"grants": []})
        assert (await client.get(path + "/preview", headers=member)).status_code == 404
        assert (
            await client.get(path + "/content", headers=_headers("reader@example.test"))
        ).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind,body,expected",
    [("html", b"x" * 501, 413), ("file", b"x" * 1001, 413), ("html", b"\xff", 422)],
)
async def test_upload_limits_and_utf8(publication_env, kind, body, expected):
    app, _, _, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await _publish(client, sid, kind=kind, content=body)).status_code == expected


@pytest.mark.asyncio
async def test_auth_csrf_cache_integrity_and_preview_headers(publication_env):
    app, _, blobs, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{sid}/artifacts",
            headers={**_headers(), "Origin": "https://untrusted.invalid"},
            data={"title": "Report"},
            files={"file": ("x", b"x")},
        )
        assert response.status_code == 403
        item = (await _publish(client, sid)).json()
        path = f"/v1/artifacts/{item['id']}"
        # Router-level auth error uses the application's OmnigentError handler in production.
        preview = await client.get(path + "/preview", headers=_headers())
        assert preview.headers["cache-control"] == "private, no-store"
        assert "sandbox allow-scripts" in preview.headers["content-security-policy"]
        assert "allow-same-origin" not in preview.headers["content-security-policy"]
        assert "connect-src 'none'" in preview.headers["content-security-policy"]
        download = await client.get(path + "/content", headers=_headers())
        assert download.headers["content-disposition"].startswith("attachment;")
        assert download.headers["x-content-type-options"] == "nosniff"
        blobs.put(f"published/0/{item['id']}", b"tampered")
        assert (await client.get(path + "/content", headers=_headers())).status_code == 503


@pytest.mark.asyncio
async def test_tombstone_expiry_and_collector_retry(publication_env):
    app, store, blobs, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        item = (await _publish(client, sid)).json()
        path = f"/v1/artifacts/{item['id']}"
        assert (await client.delete(path, headers=_headers())).status_code == 204
        assert (await client.get(path, headers=_headers())).status_code == 404
        assert blobs.exists(f"published/0/{item['id']}")
        cleanup = asyncio.create_task(collect_published_artifacts(store, blobs))
        for _ in range(200):
            if not store.garbage():
                break
            await asyncio.sleep(0.01)
        cleanup.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cleanup
        assert not blobs.exists(f"published/0/{item['id']}")
        assert not store.garbage()
        expired = PublishedArtifact(
            id="a" * 32,
            owner_id="owner@example.test",
            source_session_id=sid,
            title="Old",
            filename="x",
            kind="file",
            content_type="application/octet-stream",
            byte_size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            created_at=1,
            expires_at=2,
        )
        store.create(expired)
        assert store.get(expired.id) is None
        assert any(row.id == expired.id for _, row in store.garbage())


@pytest.mark.parametrize(
    "config",
    [
        {"retention_days": 0},
        {"max_bytes": True},
        {"unknown": 1},
        {"retention_days": 366},
        {"max_bytes": 1},
    ],
)
def test_invalid_operator_limits(config):
    with pytest.raises(ValueError):
        PublicationLimits.parse(config)


@pytest.mark.asyncio
async def test_sharing_off_allows_revocation_but_no_new_grants(publication_env):
    from omnigent.server.auth import SharingMode

    app, _, _, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        item = (await _publish(client, sid)).json()
        path = f"/v1/artifacts/{item['id']}/permissions"
        body = {"grants": [{"principal_type": "user", "principal": "reader@example.test"}]}
        assert (await client.put(path, headers=_headers(), json=body)).status_code == 200
        app.state.sharing_mode = lambda: SharingMode.OFF
        assert (await client.put(path, headers=_headers(), json={"grants": []})).status_code == 200
        assert (await client.put(path, headers=_headers(), json=body)).status_code == 403


def test_pending_publications_and_cleanup_are_serialized(publication_env):
    _, store, _, _, sid = publication_env
    from dataclasses import replace

    item = PublishedArtifact(
        id="c" * 32,
        owner_id="owner@example.test",
        source_session_id=sid,
        title="Pending",
        filename="x",
        kind="file",
        content_type="application/octet-stream",
        byte_size=0,
        sha256=hashlib.sha256(b"").hexdigest(),
        created_at=int(time.time()) - 7200,
        expires_at=int(time.time()) + 3600,
        ready=False,
    )
    store.create(item)
    assert store.get(item.id) is None
    assert any(row.id == item.id for _, row in store.garbage())
    assert store.complete(item.id)
    # A collector with an old pending-upload snapshot must not delete a finalized blob.
    assert not store.claim_garbage(0, item.id)
    assert store.get(item.id) is not None
    abandoned = replace(item, id="d" * 32)
    store.create(abandoned)
    assert store.claim_garbage(0, abandoned.id)
    assert not store.complete(abandoned.id)
    assert store.get(abandoned.id) is None


@pytest.mark.asyncio
async def test_failed_blob_write_never_publishes_and_keeps_cleanup_record(
    publication_env, monkeypatch
):
    app, store, blobs, _, sid = publication_env

    def fail(*args):
        raise OSError("storage unavailable")

    monkeypatch.setattr(blobs, "put", fail)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        with pytest.raises(OSError, match="storage unavailable"):
            await _publish(client, sid)
    assert not store.list_owned("owner@example.test")
    assert len(store.garbage()) == 1


@pytest.mark.asyncio
async def test_missing_auth_bad_grants_retention_and_filename(publication_env):
    app, _, _, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get("/v1/artifacts")).status_code == 401
        assert (await _publish(client, sid, retention_days="366")).status_code == 422
        item = (await _publish(client, sid)).json()
        for grant in [
            {"principal_type": "user", "principal": "oidc-group:ZW5naW5lZXJpbmc"},
            {"principal_type": "user", "principal": "__public__"},
            {"principal_type": "group", "principal": " engineering"},
        ]:
            response = await client.put(
                f"/v1/artifacts/{item['id']}/permissions",
                headers=_headers(),
                json={"grants": [grant]},
            )
            assert response.status_code == 422
        assert (await client.get("/v1/artifacts/invalid", headers=_headers())).status_code == 404


def test_public_origin_configuration():
    assert (
        PublicationLimits.parse({"public_url": "https://app.example"}).public_url
        == "https://app.example"
    )
    for value in (
        "http://app.example",
        "https://user:password@app.example",
        "https://app.example/extra",
        "https://app.example?key=x",
        "https://app.example:0",
        "https://app.example\n",
    ):
        with pytest.raises(ValueError):
            PublicationLimits.parse({"public_url": value})


@pytest.mark.asyncio
async def test_full_application_wires_publication_catalog(client):
    response = await client.get("/v1/artifacts")
    assert response.status_code == 200, response.text
    assert response.json() == {"data": []}
