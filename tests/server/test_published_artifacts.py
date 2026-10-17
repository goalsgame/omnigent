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

from omnigent.db.account_authority import account_authority_scope, bind_account_authority
from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import bind_group_authority, group_authority_scope
from omnigent.errors import OmnigentError
from omnigent.server.accounts_store import SqlAlchemyAccountStore
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
            generation = request.headers.get("x-test-generation")
            if generation:
                bind_account_authority(user, generation)
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
            account_authority_scope(None, None),
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


async def _share(client, path, *, headers, json):
    if "revision" not in json:
        current = await client.get(path.removesuffix("/permissions"), headers=headers)
        json = {**json, "revision": current.json().get("sharing_revision", "0" * 64)}
    return await client.put(path, headers=headers, json=json)


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
        result = await _share(
            client, path + "/permissions", headers=_headers(), json={"grants": grants}
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
            await _share(client, path + "/permissions", headers=member, json={"grants": []})
        ).status_code == 404
        assert (await client.delete(path, headers=member)).status_code == 404
        await _share(client, path + "/permissions", headers=_headers(), json={"grants": []})
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
        assert preview.headers["content-security-policy"] == "default-src 'none'; sandbox"
        assert preview.headers["content-type"].startswith("text/plain")
        assert preview.headers["content-disposition"].startswith("attachment;")
        renderer = await client.get("/v1/artifact-renderer")
        assert renderer.status_code == 200
        assert "frame-src 'none'" in renderer.headers["content-security-policy"]
        assert "sandbox allow-scripts" in renderer.headers["content-security-policy"]
        assert "<h1>report</h1>" not in renderer.text
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
        assert (await _share(client, path, headers=_headers(), json=body)).status_code == 200
        app.state.sharing_mode = lambda: SharingMode.OFF
        assert (
            await _share(client, path, headers=_headers(), json={"grants": []})
        ).status_code == 200
        assert (await _share(client, path, headers=_headers(), json=body)).status_code == 403


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
            response = await _share(
                client,
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


@pytest.mark.asyncio
async def test_links_use_configured_base_path_when_internal_request_has_no_root(publication_env):
    app, _, _, _, sid = publication_env
    app.state.base_path = "/proxy/6767"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        item = (await _publish(client, sid)).json()
        assert item["viewer_url"] == f"/proxy/6767/artifacts/{item['id']}"
        assert item["download_url"] == f"/proxy/6767/v1/artifacts/{item['id']}/content"


@pytest.mark.asyncio
async def test_app_uses_operator_publication_config_when_no_override(
    db_uri, tmp_path, monkeypatch
):
    from tests.server.routes.test_sessions_sharing_mode import _build_app

    config = {
        "published_artifacts": {
            "public_url": "https://public.example",
            "max_bytes": 100,
            "max_html_bytes": 100,
            "retention_days": 7,
            "max_retention_days": 10,
        }
    }
    monkeypatch.setattr("omnigent.server.server_config.load_server_config", lambda: config)
    app = _build_app(db_uri, tmp_path)
    conversations = SqlAlchemyConversationStore(db_uri)
    sid = conversations.create_conversation().id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://internal"
    ) as client:
        response = await _publish(client, sid)
        assert response.status_code == 201, response.text
        item = response.json()
        assert item["viewer_url"].startswith("https://public.example/artifacts/")
        assert item["expires_at"] - item["created_at"] == 7 * 86400
        assert (await _publish(client, sid, content=b"x" * 101)).status_code == 413
        assert (await _publish(client, sid, retention_days="11")).status_code == 422


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user,sid,declared,expected",
    [
        ("owner@example.test", None, "100000", 413),
        ("owner@example.test", None, "-1", 400),
        ("owner@example.test", None, "invalid", 400),
        ("reader@example.test", None, None, 403),
        (None, None, None, 401),
        ("owner@example.test", "invalid", None, 404),
    ],
)
async def test_upload_rejected_before_body_consumption(
    publication_env, user, sid, declared, expected
):
    app, _, _, _, session_id = publication_env
    consumed = []

    async def body():
        consumed.append(True)
        yield b"not parsed"

    headers = {"Content-Type": "multipart/form-data; boundary=test"}
    if user:
        headers.update(_headers(user))
    if declared is not None:
        headers["Content-Length"] = declared
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{sid or session_id}/artifacts", headers=headers, content=body()
        )
    assert response.status_code == expected
    assert not consumed


@pytest.mark.asyncio
async def test_chunked_multipart_stops_at_request_budget(publication_env):
    app, _, _, _, sid = publication_env
    consumed = []

    async def body():
        yield b'--test\r\nContent-Disposition: form-data; name="file"; filename="x"\r\n\r\n'
        for i in range(10):
            consumed.append(i)
            yield b"x" * 32768
        yield b"\r\n--test--\r\n"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{sid}/artifacts",
            headers=_headers(**{"Content-Type": "multipart/form-data; boundary=test"}),
            content=body(),
        )
    assert response.status_code == 413
    assert len(consumed) == 3


@pytest.mark.asyncio
async def test_recreated_accounts_cannot_inherit_ownership_or_user_grants(publication_env, db_uri):
    app, store, _, _, sid = publication_env
    accounts = SqlAlchemyAccountStore(db_uri)
    owner, reader = "owner@example.test", "reader@example.test"
    for user in (owner, reader):
        accounts.update_password(user, "hashed-password")
    owner_generation = accounts.login_snapshot(owner)[1]
    reader_generation = accounts.login_snapshot(reader)[1]
    assert owner_generation and reader_generation
    owner_headers = _headers(owner, **{"x-test-generation": owner_generation})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{sid}/artifacts",
            headers=owner_headers,
            data={"title": "Report", "kind": "html"},
            files={"file": ("report.html", b"<h1>private</h1>")},
        )
        assert response.status_code == 201
        item = response.json()
        assert item["can_manage"]
        path = f"/v1/artifacts/{item['id']}"
        grants = {"grants": [{"principal_type": "user", "principal": reader}]}
        assert (
            await _share(client, path + "/permissions", headers=owner_headers, json=grants)
        ).status_code == 200
        assert (
            await client.get(
                path, headers=_headers(reader, **{"x-test-generation": reader_generation})
            )
        ).status_code == 200
        assert accounts.delete_user(reader)
        accounts.create_user_with_password(reader, "replacement-password")
        replacement_reader = accounts.login_snapshot(reader)[1]
        assert replacement_reader and replacement_reader != reader_generation
        reader_headers = _headers(reader, **{"x-test-generation": replacement_reader})
        assert (await client.get(path, headers=reader_headers)).status_code == 404
        unrelated = {
            "grants": [*grants["grants"], {"principal_type": "group", "principal": "engineering"}]
        }
        assert (
            await _share(client, path + "/permissions", headers=owner_headers, json=unrelated)
        ).status_code == 200
        assert (await client.get(path, headers=reader_headers)).status_code == 404
        # Explicit regrant requires removing the retained registration first.
        assert (
            await _share(client, path + "/permissions", headers=owner_headers, json={"grants": []})
        ).status_code == 200
        assert (
            await _share(client, path + "/permissions", headers=owner_headers, json=grants)
        ).status_code == 200
        assert (await client.get(path, headers=reader_headers)).status_code == 200
        assert accounts.delete_user(owner)
        accounts.create_user_with_password(owner, "replacement-password")
        replacement_owner = accounts.login_snapshot(owner)[1]
        assert replacement_owner and replacement_owner != owner_generation
        replacement_headers = _headers(owner, **{"x-test-generation": replacement_owner})
        assert (await client.get(path, headers=replacement_headers)).status_code == 404
        assert (await client.get("/v1/artifacts", headers=replacement_headers)).json() == {
            "data": []
        }
        assert (
            await _share(client, path + "/permissions", headers=replacement_headers, json=grants)
        ).status_code == 404
        assert (await client.delete(path, headers=replacement_headers)).status_code == 404
        with account_authority_scope(owner, owner_generation), pytest.raises(OmnigentError):
            store.mutate(item["id"], owner, delete=True)


@pytest.mark.asyncio
async def test_cleanup_advances_past_full_batch_of_failures(publication_env, monkeypatch):
    _, store, blobs, _, sid = publication_env
    for index in range(101):
        item = PublishedArtifact(
            id=f"{index:032x}",
            owner_id="owner@example.test",
            source_session_id=sid,
            title="Expired",
            filename="x",
            kind="file",
            content_type="application/octet-stream",
            byte_size=0,
            sha256=hashlib.sha256(b"").hexdigest(),
            created_at=1,
            expires_at=2,
        )
        store.create(item)
        blobs.put(item.blob_key(), b"")
    healthy_key = f"published/0/{100:032x}"
    delete = blobs.delete
    attempted = []

    def selectively_delete(key):
        attempted.append(key)
        if key != healthy_key:
            raise RuntimeError("retention lock")
        delete(key)

    monkeypatch.setattr(blobs, "delete", selectively_delete)
    task = asyncio.create_task(collect_published_artifacts(store, blobs))
    try:
        async with asyncio.timeout(10):
            while blobs.exists(healthy_key) or store.garbage(after=(0, f"{99:032x}")):
                await asyncio.sleep(0.01)
        assert len(attempted) == 101
        assert len(store.garbage()) == 100
        assert not store.garbage(after=(0, f"{99:032x}"))
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_revocation_preserves_deleted_retained_grantees(publication_env, db_uri):
    app, store, _, _, sid = publication_env
    accounts = SqlAlchemyAccountStore(db_uri)
    owner, deleted, revoked = "owner@example.test", "reader@example.test", "editor@example.test"
    for user in (owner, deleted, revoked):
        accounts.update_password(user, "hashed-password")
    owner_headers = _headers(owner, **{"x-test-generation": accounts.login_snapshot(owner)[1]})
    revoked_headers = _headers(
        revoked, **{"x-test-generation": accounts.login_snapshot(revoked)[1]}
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            f"/v1/sessions/{sid}/artifacts",
            headers=owner_headers,
            data={"title": "Report"},
            files={"file": ("report.txt", b"private")},
        )
        path = f"/v1/artifacts/{response.json()['id']}"
        grant_a = {"principal_type": "user", "principal": deleted}
        grant_b = {"principal_type": "user", "principal": revoked}
        assert (
            await _share(
                client,
                path + "/permissions",
                headers=owner_headers,
                json={"grants": [grant_a, grant_b]},
            )
        ).status_code == 200
        assert (await client.get(path, headers=revoked_headers)).status_code == 200
        assert accounts.delete_user(deleted)
        assert (
            await _share(
                client, path + "/permissions", headers=owner_headers, json={"grants": [grant_a]}
            )
        ).status_code == 200
        assert (await client.get(path, headers=revoked_headers)).status_code == 404
        # Removed retained accounts cannot be added anew until active again.
        assert (
            await _share(client, path + "/permissions", headers=owner_headers, json={"grants": []})
        ).status_code == 200
        invalid = await _share(
            client, path + "/permissions", headers=owner_headers, json={"grants": [grant_a]}
        )
        assert invalid.status_code == 409
        assert (await client.get(path, headers=owner_headers)).status_code == 200
        # A stale full-list write cannot silently overwrite a concurrent edit.
        current = store.get(response.json()["id"])
        assert current
        with (
            account_authority_scope(owner, accounts.login_snapshot(owner)[1]),
            pytest.raises(OmnigentError) as conflict,
        ):
            store.mutate(current.id, owner, grants=[], expected_grants_json="stale")
        assert conflict.value.http_status == 409


@pytest.mark.asyncio
async def test_stale_client_sharing_cannot_restore_revoked_reader(publication_env):
    app, _, _, _, sid = publication_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        item = (await _publish(client, sid)).json()
        path = f"/v1/artifacts/{item['id']}/permissions"
        reader = {"principal_type": "user", "principal": "reader@example.test"}
        first = await _share(client, path, headers=_headers(), json={"grants": [reader]})
        stale = first.json()["sharing_revision"]
        assert (
            await _share(client, path, headers=_headers(), json={"grants": [], "revision": stale})
        ).status_code == 200
        response = await _share(
            client,
            path,
            headers=_headers(),
            json={
                "grants": [reader, {"principal_type": "group", "principal": "engineering"}],
                "revision": stale,
            },
        )
        assert response.status_code == 409
        assert (
            await client.get(
                path.removesuffix("/permissions"), headers=_headers("reader@example.test")
            )
        ).status_code == 404
        missing = await client.put(path, headers=_headers(), json={"grants": []})
        assert missing.status_code == 422
