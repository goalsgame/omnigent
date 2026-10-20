"""Durable peer delivery, caller attribution and session permission boundaries."""

from __future__ import annotations

import json
import time
import uuid

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.db.group_authority import bind_group_authority, group_authority_scope
from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes.peer_messages import create_peer_messages_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_message_store import PeerMessageStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class _Auth(AuthProvider):
    def get_user_id(self, request):
        user = request.headers.get("x-user")
        if user:
            bind_group_authority(
                user, json.loads(request.headers.get("x-groups", "[]")), int(time.time()) + 60
            )
        return user


@pytest.fixture
def peer_env(db_uri):
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    store = PeerMessageStore(db_uri)
    source = conversations.create_conversation().id
    target = conversations.create_conversation().id
    other = conversations.create_conversation().id
    for user in (
        "alice@example.test",
        "bob@example.test",
        "reader@example.test",
        "oidc-machine:bot",
    ):
        permissions.ensure_user(user)
    for sid in (source, target, other):
        permissions.grant("alice@example.test", sid, 4)
        permissions.grant("oidc-machine:bot", sid, 2)
        permissions.grant("reader@example.test", sid, 1)
    permissions.grant("bob@example.test", target, 4)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=exc.http_status)

    @app.middleware("http")
    async def authority(request: Request, call_next):
        with (
            account_authority_scope(None, None),
            group_authority_scope(),
            workspace_scope(int(request.headers.get("x-workspace", "0"))),
        ):
            return await call_next(request)

    app.include_router(
        create_peer_messages_router(store, conversations, permissions, _Auth()), prefix="/v1"
    )
    return app, store, conversations, permissions, source, target, other


def _headers(user="alice@example.test", **extra):
    return {"x-user": user, **extra}


def _message(target, **extra):
    return {
        "id": uuid.uuid4().hex,
        "recipient_session_id": target,
        "subject": "Coordination",
        "body": "Please check the artifact link.",
        **extra,
    }


def _path(sid):
    return f"/v1/sessions/{sid}/peer-messages"


@pytest.mark.asyncio
async def test_durable_send_reply_acknowledgement_and_reinstantiation(peer_env, db_uri):
    app, store, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        message = _message(target)
        response = await client.post(_path(source), json=message, headers=_headers())
        assert response.status_code == 201, response.text
        item = response.json()
        assert (
            item["sender_session_id"] == source and item["sender_user_id"] == "alice@example.test"
        )
        assert item["expires_at"] - item["created_at"] == 90 * 86400 * 1_000_000
        # A new store instance sees the committed inbox without a runner or process cache.
        assert PeerMessageStore(db_uri).list_messages(target)[0].id == item["id"]
        inbox = await client.get(_path(target), headers=_headers("bob@example.test"))
        assert inbox.status_code == 200 and inbox.json()["messages"][0]["acknowledged_at"] is None
        assert inbox.headers["cache-control"] == "private, no-store"
        reply = await client.post(
            _path(target), json=_message(source, reply_to=item["id"]), headers=_headers()
        )
        assert reply.status_code == 201
        for _ in range(2):
            assert (
                await client.post(
                    _path(target) + f"/{item['id']}/acknowledge",
                    headers=_headers("bob@example.test"),
                )
            ).status_code == 200
        assert store.list_messages(target, unread_only=True) == []
        assert store.list_messages(source)[0].reply_to == item["id"]
        assert store.list_messages(source, sent=True)[0].id == item["id"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user,expected",
    [
        (None, 401),
        ("reader@example.test", 403),
        ("bob@example.test", 404),
        ("oidc-machine:bot", 201),
    ],
)
async def test_send_requires_edit_on_both_sessions(peer_env, user, expected):
    app, _, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            _path(source), json=_message(target), headers=_headers(user) if user else {}
        )
        assert response.status_code == expected, response.text
        if expected == 201:
            assert response.json()["sender_user_id"] == user


@pytest.mark.asyncio
async def test_readers_cannot_send_or_ack_and_strangers_cannot_read(peer_env):
    app, _, _, _, source, target, other = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        item = (await client.post(_path(source), json=_message(target), headers=_headers())).json()
        assert (
            await client.get(_path(target), headers=_headers("reader@example.test"))
        ).status_code == 200
        assert (
            await client.post(
                _path(target) + f"/{item['id']}/acknowledge",
                headers=_headers("reader@example.test"),
            )
        ).status_code == 403
        assert (
            await client.get(_path(source), headers=_headers("bob@example.test"))
        ).status_code == 404
        assert (
            await client.post(_path(other) + f"/{item['id']}/acknowledge", headers=_headers())
        ).status_code == 404
        assert (
            await client.post(_path(source) + f"/{item['id']}/acknowledge", headers=_headers())
        ).status_code == 404


@pytest.mark.asyncio
async def test_idempotency_conflicts_invalid_replies_and_spoofed_sender(peer_env):
    app, store, _, _, source, target, other = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        data = _message(target)
        first = await client.post(_path(source), json=data, headers=_headers())
        second = await client.post(_path(source), json=data, headers=_headers())
        assert first.json() == second.json()
        assert len(store.list_messages(target)) == 1
        assert (
            await client.post(
                _path(source), json={**data, "body": "different"}, headers=_headers()
            )
        ).status_code == 409
        assert (
            await client.post(
                _path(source), json=_message(other, reply_to=data["id"]), headers=_headers()
            )
        ).status_code == 404
        assert (
            await client.post(
                _path(target), json=_message(source, reply_to=uuid.uuid4().hex), headers=_headers()
            )
        ).status_code == 404
        assert (
            await client.post(_path(source), json=_message(source), headers=_headers())
        ).status_code == 422
        assert (
            await client.post(
                _path(source),
                json=_message(target, sender_user_id="bob@example.test"),
                headers=_headers(),
            )
        ).status_code == 422


@pytest.mark.asyncio
async def test_pagination_unread_and_new_arrival_after_ack(peer_env):
    app, store, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        ids = []
        for _ in range(3):
            ids.append(
                (
                    await client.post(_path(source), json=_message(target), headers=_headers())
                ).json()["id"]
            )
        first = (await client.get(_path(target), params={"limit": 2}, headers=_headers())).json()
        second = (
            await client.get(
                _path(target), params={"limit": 2, "cursor": first["cursor"]}, headers=_headers()
            )
        ).json()
        assert first["has_more"] and not second["has_more"]
        assert [m["id"] for m in first["messages"] + second["messages"]] == ids
        assert (
            await client.get(_path(target), params={"cursor": "broken"}, headers=_headers())
        ).status_code == 422
        assert (
            await client.get(
                _path(target), params={"cursor": "9" * 30 + ":" + ids[0]}, headers=_headers()
            )
        ).status_code == 422

        await client.post(_path(target) + f"/{ids[0]}/acknowledge", headers=_headers())
        assert [m.id for m in store.list_messages(target, unread_only=True)] == ids[1:]


@pytest.mark.asyncio
async def test_revocation_groups_workspace_isolation_and_session_deletion(peer_env):
    from omnigent.db.group_authority import group_principal

    app, store, conversations, permissions, source, target, _ = peer_env
    permissions.grant(group_principal("team"), source, 2, is_group=True)
    permissions.grant(group_principal("team"), target, 2, is_group=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        group_headers = _headers("bob@example.test", **{"x-groups": '["team"]'})
        data = _message(target)
        assert (
            await client.post(_path(source), json=data, headers=group_headers)
        ).status_code == 201
        assert (
            await client.post(
                _path(source), json=_message(target), headers=_headers("bob@example.test")
            )
        ).status_code == 404
        assert (
            await client.get(_path(target), headers=_headers(**{"x-workspace": "987"}))
        ).status_code == 404
        with workspace_scope(987):
            assert store.list_messages(target) == []
        permissions.revoke(group_principal("team"), source)
        assert (
            await client.post(_path(source), json=_message(target), headers=group_headers)
        ).status_code == 404
        await conversations.delete_conversation(target)
        assert (await client.get(_path(target), headers=_headers())).status_code == 404


@pytest.mark.asyncio
@pytest.mark.parametrize("body", ["a" * 8193, "🦄" * 2049], ids=["ascii", "unicode"])
async def test_body_byte_limits(peer_env, body):
    app, _, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            _path(source), json=_message(target, body=body), headers=_headers()
        )
        assert response.status_code == 422


@pytest.mark.asyncio
async def test_streaming_body_limit_and_origin_check(peer_env):
    app, _, _, _, source, target, _ = peer_env

    async def chunks():
        for _ in range(8):
            yield b" " * 10000

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.post(
                _path(source),
                content=chunks(),
                headers=_headers(**{"content-type": "application/json"}),
            )
        ).status_code == 413
        assert (
            await client.post(
                _path(source),
                json=_message(target),
                headers=_headers(origin="https://evil.invalid"),
            )
        ).status_code == 403


@pytest.mark.asyncio
async def test_expiry_is_unavailable_and_cleaned_on_send(peer_env):
    from omnigent.db.db_models import SqlPeerMessage, current_workspace_id

    app, store, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        data = _message(target)
        await client.post(_path(source), json=data, headers=_headers())
        with store._write("expire_test_message") as session:
            row = session.get(SqlPeerMessage, (current_workspace_id(), data["id"]))
            row.expires_at = 0
        assert store.list_messages(target) == []
        assert (
            await client.post(_path(target) + f"/{data['id']}/acknowledge", headers=_headers())
        ).status_code == 404
        assert (
            await client.post(_path(source), json=_message(target), headers=_headers())
        ).status_code == 201
        with store._read("verify_expired_removed") as session:
            assert session.get(SqlPeerMessage, (current_workspace_id(), data["id"])) is None


@pytest.mark.asyncio
async def test_concurrent_duplicate_send_and_ack_keep_one_message(peer_env):
    import asyncio

    app, store, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        data = _message(target)
        sends = await asyncio.gather(
            *(client.post(_path(source), json=data, headers=_headers()) for _ in range(4))
        )
        assert [r.status_code for r in sends] == [201] * 4
        assert len(store.list_messages(target)) == 1
        acks = await asyncio.gather(
            *(
                client.post(_path(target) + f"/{data['id']}/acknowledge", headers=_headers())
                for _ in range(4)
            )
        )
        assert [r.status_code for r in acks] == [200] * 4
        assert store.list_messages(target)[0].acknowledged_at is not None


@pytest.mark.asyncio
async def test_native_dispatch_to_real_api_and_persisted_inbox(peer_env):
    from omnigent.runner.tool_dispatch import execute_tool
    from omnigent.spec.types import AgentSpec

    app, store, _, _, source, target, _ = peer_env
    spec = AgentSpec(spec_version=1)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test", headers=_headers()
    ) as client:
        sent = json.loads(
            await execute_tool(
                tool_name="sys_peer_send",
                arguments=json.dumps(
                    {
                        "recipient_session_id": target,
                        "subject": "handoff",
                        "body": "The artifact is ready.",
                    }
                ),
                conversation_id=source,
                server_client=client,
                agent_spec=spec,
            )
        )
        assert "error" not in sent, sent
        received = json.loads(
            await execute_tool(
                tool_name="sys_peer_read",
                arguments="{}",
                conversation_id=target,
                server_client=client,
                agent_spec=spec,
            )
        )
        assert received["result"]["messages"][0]["body"] == "The artifact is ready."
        await execute_tool(
            tool_name="sys_peer_acknowledge",
            arguments=json.dumps({"message_id": sent["message_id"]}),
            conversation_id=target,
            server_client=client,
            agent_spec=spec,
        )
        assert store.list_messages(target, unread_only=True) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("recreate_source", [False, True], ids=["recipient", "sender"])
async def test_delete_reimport_by_another_owner_cannot_recover_mailbox(peer_env, recreate_source):
    from omnigent.server.routes.imports import _import_conversation_id

    app, store, conversations, permissions, source, target, _ = peer_env
    reused = _import_conversation_id("codex", "previously-imported-session")
    conversations.create_conversation(conversation_id=reused)
    permissions.grant("alice@example.test", reused, 4)
    source, target = (reused, target) if recreate_source else (source, reused)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        data = _message(target)
        original = await client.post(_path(source), json=data, headers=_headers())
        assert original.status_code == 201
        old_mailbox = store.mailbox_id(reused)
        await conversations.delete_conversation(reused)
        conversations.create_conversation(conversation_id=reused)
        permissions.grant("bob@example.test", reused, 4)
        permissions.grant("bob@example.test", target if recreate_source else source, 2)
        assert store.mailbox_id(reused) != old_mailbox
        for direction in ("received", "sent"):
            response = await client.get(
                _path(reused),
                params={"direction": direction},
                headers=_headers("bob@example.test"),
            )
            assert response.status_code == 200 and response.json()["messages"] == []
        assert (
            await client.post(
                _path(reused) + f"/{data['id']}/acknowledge", headers=_headers("bob@example.test")
            )
        ).status_code == 404
        assert (
            await client.post(
                _path(target),
                json=_message(source, reply_to=data["id"]),
                headers=_headers("bob@example.test"),
            )
        ).status_code == 404


@pytest.mark.asyncio
async def test_send_and_read_with_stale_mailbox_incarnation_fail_closed(peer_env):
    from dataclasses import replace

    app, store, conversations, permissions, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(_path(source), json=_message(target), headers=_headers())
    old = store.list_messages(target)[0]
    await conversations.delete_conversation(target)
    conversations.create_conversation(conversation_id=target)
    permissions.grant("alice@example.test", target, 4)
    with pytest.raises(OmnigentError, match="Session changed"):
        store.send(replace(old, id=uuid.uuid4().hex))
    assert store.list_messages(target, mailbox_id=old.recipient_mailbox_id) == []
    assert not store.acknowledge(
        target, old.id, "alice@example.test", mailbox_id=old.recipient_mailbox_id
    )


@pytest.mark.asyncio
async def test_legacy_mailboxes_initialize_only_on_authorized_write(peer_env):
    from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id

    app, store, _, _, source, target, _ = peer_env
    with store._write("prepare_legacy_sessions") as session:
        for sid in (source, target):
            session.get(
                SqlConversationMetadata, (current_workspace_id(), sid)
            ).peer_mailbox_id = None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        read = await client.get(_path(target), headers=_headers("reader@example.test"))
        assert read.status_code == 200 and read.json()["messages"] == []
        assert store.mailbox_id(target) is None
        assert (
            await client.post(
                _path(source), json=_message(target), headers=_headers("reader@example.test")
            )
        ).status_code == 403
        assert store.mailbox_id(source) is None
        assert (
            await client.post(_path(source), json=_message(target), headers=_headers())
        ).status_code == 201
        assert store.mailbox_id(source) and store.mailbox_id(target)
        assert store.mailbox_id(source) != store.mailbox_id(target)
        with pytest.raises(OmnigentError, match="Session changed"):
            store.initialize_mailbox_id(target, "alice@example.test")


@pytest.mark.asyncio
async def test_delayed_send_and_backward_clock_cannot_land_behind_cursor(peer_env, monkeypatch):
    from dataclasses import replace

    app, store, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = (
            await client.post(_path(source), json=_message(target), headers=_headers())
        ).json()
        # Capture an old request before later requests are committed and paginated.
        delayed = replace(
            store.list_messages(target)[0],
            id=uuid.uuid4().hex,
            created_at=first["created_at"] - 100_000,
            expires_at=first["expires_at"] - 100_000,
        )
        await client.post(_path(source), json=_message(target), headers=_headers())
        await client.post(_path(source), json=_message(target), headers=_headers())
        page = (await client.get(_path(target), params={"limit": 2}, headers=_headers())).json()
        assert page["has_more"]
        monkeypatch.setattr(
            "omnigent.stores.peer_message_store.now_epoch_us",
            lambda: first["created_at"] - 200_000,
        )
        committed = store.send(delayed)
        assert committed.created_at > int(page["cursor"].split(":")[0])
        remaining = (
            await client.get(_path(target), params={"cursor": page["cursor"]}, headers=_headers())
        ).json()
        assert committed.id in {item["id"] for item in remaining["messages"]}
        assert remaining["messages"][-1]["id"] == committed.id
        # Identical retries retain the original ordering key and receipt.
        assert store.send(delayed) == committed
        from sqlalchemy import update

        from omnigent.db.db_models import SqlPeerMessage, current_workspace_id

        with store._write("expire_mailbox_history") as session:
            session.execute(
                update(SqlPeerMessage)
                .where(SqlPeerMessage.workspace_id == current_workspace_id())
                .values(expires_at=0)
            )
        after_cleanup = store.send(replace(delayed, id=uuid.uuid4().hex))
        assert after_cleanup.created_at > committed.created_at
        assert [row.id for row in store.list_messages(target)] == [after_cleanup.id]


@pytest.mark.asyncio
async def test_notification_claims_retry_and_ack_are_separate(peer_env, db_uri):
    from dataclasses import replace

    from sqlalchemy import update

    from omnigent.db.db_models import SqlPeerDelivery
    from omnigent.db.utils import get_or_create_engine
    from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

    app, messages, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        message = (
            await client.post(_path(source), json=_message(target), headers=_headers())
        ).json()
    store = PeerDeliveryStore(db_uri)
    mailbox = messages.mailbox_id(target)
    default = DeliveryPolicy()
    batch = store.claim(target, mailbox, default)
    assert batch.message_ids == (message["id"],)
    assert PeerDeliveryStore(db_uri).claim(target, mailbox, default) is None
    assert store.finish(replace(batch, lease="f" * 32), accepted=True) is False
    assert store.finish(batch, accepted=False)
    with get_or_create_engine(db_uri).begin() as conn:
        conn.execute(update(SqlPeerDelivery).values(retry_at=0))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(_path(source), json=_message(target), headers=_headers())
    retry = store.claim(target, mailbox, default)
    assert retry.id == batch.id
    assert retry.lease != batch.lease
    assert store.finish(batch, accepted=True) is False
    assert store.finish(retry, accepted=True)
    assert messages.list_messages(target, unread_only=True)[0].acknowledged_at is None
    assert store.claim(target, mailbox, default) is None


@pytest.mark.asyncio
async def test_notification_policy_and_budget_survive_store_restart(peer_env, db_uri):
    from sqlalchemy import update

    from omnigent.db.db_models import SqlPeerDelivery
    from omnigent.db.utils import get_or_create_engine
    from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

    app, messages, _, _, source, target, _ = peer_env
    store = PeerDeliveryStore(db_uri)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(_path(source), json=_message(target), headers=_headers())
        mailbox = messages.mailbox_id(target)
        store.set_policy(target, mailbox, "alice@example.test", DeliveryPolicy(enabled=False))
        assert store.claim(target, mailbox, DeliveryPolicy()) is None
        store.set_policy(
            target, mailbox, "alice@example.test", DeliveryPolicy(wake_on_message=True)
        )
        assert PeerDeliveryStore(db_uri).policy(target, mailbox, DeliveryPolicy()).wake_on_message
        for _ in range(6):
            batch = store.claim(target, mailbox, DeliveryPolicy())
            assert batch is not None
            assert store.finish(batch, accepted=True)
            await client.post(_path(source), json=_message(target), headers=_headers())
            with get_or_create_engine(db_uri).begin() as conn:
                conn.execute(update(SqlPeerDelivery).values(retry_at=0))
        assert PeerDeliveryStore(db_uri).claim(target, mailbox, DeliveryPolicy()) is None


@pytest.mark.asyncio
async def test_notification_owner_settings_enforce_origin_strict_types_and_visibility(
    peer_env, db_uri
):
    from types import SimpleNamespace

    from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

    _, _, conversations, permissions, source, target, _ = peer_env
    store = PeerDeliveryStore(db_uri)
    app = FastAPI()
    app.include_router(
        create_peer_messages_router(
            store,
            conversations,
            permissions,
            _Auth(),
            notifier=SimpleNamespace(
                store=store, defaults=lambda conv: DeliveryPolicy(), supported=lambda conv: True
            ),
        ),
        prefix="/v1",
    )

    @app.exception_handler(OmnigentError)
    async def handle_error(request, exc):
        return JSONResponse({"error": str(exc)}, status_code=exc.http_status)

    path = f"/v1/sessions/{target}/peer-notifications"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (await client.get(path, headers=_headers("reader@example.test"))).json()[
            "can_manage"
        ] is False
        assert (
            await client.put(
                path,
                json={"enabled": True, "wake_on_message": True},
                headers=_headers("reader@example.test"),
            )
        ).status_code == 403
        assert (
            await client.put(
                path,
                json={"enabled": True, "wake_on_message": True},
                headers=_headers(origin="https://untrusted.example.test"),
            )
        ).status_code == 403
        assert (
            await client.put(
                path, json={"enabled": True, "wake_on_message": "true"}, headers=_headers()
            )
        ).status_code == 422
        result = await client.put(
            path, json={"enabled": True, "wake_on_message": True}, headers=_headers()
        )
        assert result.status_code == 200, result.text
        assert (await client.get(path, headers=_headers())).json()["wake_on_message"] is True
        assert store.policy(source, "f" * 32, DeliveryPolicy()).wake_on_message is False


@pytest.mark.asyncio
async def test_notification_scan_scopes_workspace_and_ignores_acknowledged_mail(peer_env, db_uri):
    from omnigent.stores.peer_delivery_store import PeerDeliveryStore

    app, messages, _, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        mid = (await client.post(_path(source), json=_message(target), headers=_headers())).json()[
            "id"
        ]
    store = PeerDeliveryStore(db_uri)
    assert store.candidates() == [(0, target, messages.mailbox_id(target))]
    with workspace_scope(12):
        assert messages.list_messages(target) == []
    messages.acknowledge(target, mid, "bob@example.test")
    assert store.candidates() == []


@pytest.mark.asyncio
async def test_notifier_retries_until_runner_receipt_without_acknowledging(
    peer_env, db_uri, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from sqlalchemy import update

    from omnigent.db.db_models import SqlPeerDelivery
    from omnigent.db.utils import get_or_create_engine
    from omnigent.server import peer_notifications
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

    app, messages, conversations, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        mid = (await client.post(_path(source), json=_message(target), headers=_headers())).json()[
            "id"
        ]
    store = PeerDeliveryStore(db_uri)
    posts = []

    async def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json={"protocol_version": 1})
        posts.append(json.loads(request.content))
        return httpx.Response(202 if len(posts) == 1 else 200, json={"delivered": len(posts) > 1})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://runner"
    ) as runner:
        monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=runner))
        monkeypatch.setattr(
            orchestration, "_ensure_runner_session_initialized", AsyncMock(return_value=True)
        )
        notifier = peer_notifications.PeerNotifier(
            store, SimpleNamespace(runner_router=None), conversations, None, None
        )
        monkeypatch.setattr(notifier, "defaults", lambda conv: DeliveryPolicy())
        monkeypatch.setattr(notifier, "supported", lambda conv: True)
        monkeypatch.setattr(sessions, "_ensure_runner_relay_ready", AsyncMock(return_value=None))
        mailbox = messages.mailbox_id(target)
        await notifier.deliver(target, mailbox)
        with get_or_create_engine(db_uri).begin() as conn:
            conn.execute(update(SqlPeerDelivery).values(retry_at=0))
        await notifier.deliver(target, mailbox)
    assert len(posts) == 2
    assert posts[0] == posts[1]
    assert posts[0]["type"] == "peer_notification" and posts[0]["message_ids"] == [mid]
    assert "body" not in posts[0] and "subject" not in posts[0]
    assert messages.list_messages(target, unread_only=True)[0].acknowledged_at is None
    assert store.candidates() == []


@pytest.mark.asyncio
async def test_notifier_does_not_wake_by_default(peer_env, db_uri, monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from omnigent.server.peer_notifications import PeerNotifier
    from omnigent.server.routes import sessions
    from omnigent.server.routes._sessions import orchestration
    from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

    app, messages, conversations, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(_path(source), json=_message(target), headers=_headers())
    store = PeerDeliveryStore(db_uri)
    monkeypatch.setattr(sessions, "_get_runner_client", AsyncMock(return_value=None))
    wake = AsyncMock(return_value=(None, conversations.get_conversation(target)))
    monkeypatch.setattr(orchestration, "ensure_runner_connected", wake)
    notifier = PeerNotifier(store, SimpleNamespace(runner_router=None), conversations, None, None)
    monkeypatch.setattr(notifier, "defaults", lambda conv: DeliveryPolicy())
    monkeypatch.setattr(notifier, "supported", lambda conv: True)
    await notifier.deliver(target, messages.mailbox_id(target))
    wake.assert_not_called()
    store.set_policy(
        target,
        messages.mailbox_id(target),
        "alice@example.test",
        DeliveryPolicy(wake_on_message=True),
    )
    await notifier.deliver(target, messages.mailbox_id(target))
    wake.assert_awaited_once()


def test_notification_selects_child_policy_and_executor():
    from types import SimpleNamespace

    from omnigent.server.peer_notifications import PeerNotifier
    from omnigent.spec.types import AgentSpec, ExecutorSpec

    child = AgentSpec(
        spec_version=1,
        name="child",
        params={"peer_messages": {"enabled": False}},
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )
    root = AgentSpec(
        spec_version=1,
        name="root",
        sub_agents=[child],
        executor=ExecutorSpec(type="omnigent", config={"harness": "openai-agents"}),
    )
    agent = SimpleNamespace(id="agent", bundle_location="bundle", operator_authored=False)
    notifier = PeerNotifier(
        None,
        None,
        None,
        SimpleNamespace(get=lambda _: agent),
        SimpleNamespace(load=lambda *a, **k: SimpleNamespace(spec=root)),
    )
    conv = SimpleNamespace(agent_id="agent", sub_agent_name="child", harness_override=None)
    assert notifier.supported(conv)
    assert not notifier.defaults(conv).enabled
    conv.sub_agent_name = None
    assert not notifier.supported(conv)


@pytest.mark.asyncio
async def test_wrong_replica_does_not_write_backoff_or_consume_budget(
    peer_env, db_uri, monkeypatch
):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from omnigent.errors import ErrorCode
    from omnigent.server.peer_notifications import PeerNotifier
    from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

    app, messages, conversations, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(_path(source), json=_message(target), headers=_headers())
    store = PeerDeliveryStore(db_uri)
    router = SimpleNamespace(
        client_for_session_resources=Mock(
            side_effect=OmnigentError("Other replica", code=ErrorCode.WRONG_REPLICA)
        )
    )
    notifier = PeerNotifier(
        store, SimpleNamespace(runner_router=router), conversations, None, None
    )
    monkeypatch.setattr(notifier, "supported", lambda conv: True)
    monkeypatch.setattr(notifier, "defaults", lambda conv: DeliveryPolicy(wake_on_message=True))
    mailbox = messages.mailbox_id(target)
    await notifier.deliver(target, mailbox)
    assert store.candidates() == [(0, target, mailbox)]
    batch = store.claim(target, mailbox, DeliveryPolicy())
    assert batch is not None


@pytest.mark.asyncio
async def test_older_runner_is_not_sent_an_unrecognized_notification(
    peer_env, db_uri, monkeypatch
):
    from types import SimpleNamespace

    from sqlalchemy import select

    from omnigent.db.db_models import SqlPeerDelivery
    from omnigent.db.utils import get_or_create_engine
    from omnigent.server.peer_notifications import PeerNotifier
    from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

    app, messages, conversations, _, source, target, _ = peer_env
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await client.post(_path(source), json=_message(target), headers=_headers())
    requests = []

    async def transport(request):
        requests.append(request)
        return httpx.Response(404)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(transport), base_url="http://runner"
    ) as runner:
        state = SimpleNamespace(
            runner_router=SimpleNamespace(
                client_for_session_resources=lambda *a, **kw: SimpleNamespace(client=runner)
            )
        )
        store = PeerDeliveryStore(db_uri)
        notifier = PeerNotifier(store, state, conversations, None, None)
        monkeypatch.setattr(notifier, "supported", lambda conv: True)
        monkeypatch.setattr(notifier, "defaults", lambda conv: DeliveryPolicy())
        await notifier.deliver(target, messages.mailbox_id(target))
    assert [request.method for request in requests] == ["GET"]
    with get_or_create_engine(db_uri).connect() as conn:
        assert conn.scalar(select(SqlPeerDelivery.attempts)) == 0
    assert messages.list_messages(target, unread_only=True)
