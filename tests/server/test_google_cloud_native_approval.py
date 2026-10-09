"""Native consent keeps the credential boundary while parking cloud commands."""

import asyncio
import time
from types import SimpleNamespace

import jwt
import pytest
from fastapi import Request
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlConversationMetadata, workspace_scope
from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.runtime import pending_elicitations
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.google_cloud_approval import (
    _finish,
    _pending,
    cloud_command,
    preflight_google_cloud,
    request_google_cloud_access,
)
from omnigent.server.routes._sessions.orchestration import _resolve_elicitation
from tests.server import test_google_cloud_session_access as access_tests
from tests.server.test_google_cloud_session_access import OWNER, UserAuth

setup = access_tests.setup


def request_for(s, user=OWNER):
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/sessions/x/events",
            "headers": [(b"x-test-user", user.encode())],
            "app": s.client.app,
        }
    )


@pytest.fixture(autouse=True)
def clear_prompts():
    yield
    for pending in list(_pending.values()):
        _finish(pending, False, "cancel")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["accept", "decline", "cancel"])
async def test_native_resolution_resumes_or_denies_command(setup, action):
    s = setup
    pending = await request_google_cloud_access(s.store, s.host)
    assert pending is not None
    assert await request_google_cloud_access(s.store, s.host) is pending
    events = pending_elicitations.snapshot_for(s.session)
    assert len(events) == 1
    assert events[0]["params"]["policy_name"] == "Google Cloud access"
    command = asyncio.create_task(
        preflight_google_cloud(
            request_for(s),
            s.host,
            {"name": "bash", "arguments": {"command": "gcloud projects list"}},
        )
    )
    await asyncio.sleep(0)
    assert not command.done()
    await _resolve_elicitation(
        s.session,
        {"elicitation_id": pending.id, "action": action},
        None,
        approval_request=request_for(s),
        approval_auth=UserAuth(),
    )
    result = await asyncio.wait_for(command, 2)
    assert (result is None) == (action == "accept")
    assert s.store.access.host(s.host, OWNER) == ("allowed" if action == "accept" else "denied")
    assert pending_elicitations.count_for(s.session) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("user", ["collaborator@example.com", "oidc-machine:bot", "local"])
async def test_collaborators_and_machines_cannot_resolve(setup, user):
    pending = await request_google_cloud_access(setup.store, setup.host)
    with pytest.raises(OmnigentError):
        await _resolve_elicitation(
            setup.session,
            {"elicitation_id": pending.id, "action": "accept"},
            None,
            approval_request=request_for(setup, user),
            approval_auth=UserAuth(),
        )
    assert not pending.future.done()
    assert setup.store.access.host(setup.host, OWNER) == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["reconnect", "revoke", "rebind", "workspace"])
async def test_stale_context_cannot_grant(setup, change):
    s = setup
    pending = await request_google_cloud_access(s.store, s.host)
    if change == "reconnect":
        s.connect()
    elif change == "revoke":
        s.store.access.session(
            s.session, OWNER, decision="denied", generation=pending.context["generation"]
        )
    elif change == "rebind":
        with Session(get_or_create_engine(s.uri)) as db:
            row = db.get(SqlConversationMetadata, (0, s.session))
            row.host_id = None
            db.commit()
    if change == "workspace":
        with workspace_scope(19), pytest.raises(OmnigentError):
            await _resolve_elicitation(
                s.session,
                {"elicitation_id": pending.id, "action": "accept"},
                None,
                approval_request=request_for(s),
                approval_auth=UserAuth(),
            )
    else:
        with pytest.raises(OmnigentError):
            await _resolve_elicitation(
                s.session,
                {"elicitation_id": pending.id, "action": "accept"},
                None,
                approval_request=request_for(s),
                approval_auth=UserAuth(),
            )
    assert s.store.access.host(s.host, OWNER) != "allowed"


@pytest.mark.asyncio
async def test_expiry_and_missing_human_request_fail_closed(setup):
    pending = await request_google_cloud_access(setup.store, setup.host)
    with pytest.raises(OmnigentError):
        await _resolve_elicitation(
            setup.session, {"elicitation_id": pending.id, "action": "accept"}, None
        )
    _finish(pending, False, "cancel")
    with pytest.raises(OmnigentError):
        await _resolve_elicitation(
            setup.session,
            {"elicitation_id": pending.id, "action": "accept"},
            None,
            approval_request=request_for(setup),
            approval_auth=UserAuth(),
        )
    assert not pending.future.result()
    assert pending_elicitations.count_for(setup.session) == 0


@pytest.mark.parametrize("extra", [{"scope": "agent"}, {"credential_delegate": True}])
def test_delegated_authority_cannot_approve_even_after_identity_cache_hit(extra):
    secret = b"s" * 32
    config = SimpleNamespace(cookie_secret=secret, session_cookie_name="session")
    auth = UnifiedAuthProvider(source="oidc", oidc_config=config)
    token = jwt.encode(
        {"sub": OWNER, "exp": time.time() + 300, **extra}, secret, algorithm="HS256"
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/sessions/x/events",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )
    assert auth.get_user_id(request) == OWNER
    assert auth.get_credential_user_id(request) is None


def test_first_party_login_grant_can_approve():
    secret = b"s" * 32
    auth = UnifiedAuthProvider(
        source="oidc",
        oidc_config=SimpleNamespace(cookie_secret=secret, session_cookie_name="session"),
    )
    token = jwt.encode(
        {"sub": OWNER, "exp": time.time() + 300, "grant_id": "login"}, secret, algorithm="HS256"
    )
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/sessions/x/events",
            "headers": [(b"authorization", f"Bearer {token}".encode())],
        }
    )
    assert auth.get_credential_user_id(request) == OWNER
    runner_token = auth.mint_runner_token(OWNER, 300)
    request.scope["headers"] = [(b"authorization", f"Bearer {runner_token}".encode())]
    assert auth.get_credential_user_id(Request(request.scope)) is None


@pytest.mark.parametrize(
    "command, expected",
    [
        ("gcloud projects list", True),
        ("/opt/bin/gcloud compute instances list", True),
        ("cd infra && terraform -chdir=app plan", True),
        ("terraform apply plan", True),
        ("terraform version", False),
        ("gcloud --version", False),
        ("ls -la", False),
        ("echo gcloud projects list", False),
        ("env X=1 bash -c 'gcloud projects list'", True),
        ("terraform init", True),
    ],
)
def test_cloud_cli_preflight(command, expected):
    assert cloud_command({"arguments": {"command": command}}) is expected


@pytest.mark.asyncio
async def test_stop_cancels_gate_and_cannot_be_approved_later(setup, monkeypatch):
    from omnigent.server.google_cloud_approval import cancel_google_cloud_access

    pending = await request_google_cloud_access(setup.store, setup.host)
    attached = asyncio.Event()

    async def attach(store, host_id):
        attached.set()
        return pending

    monkeypatch.setattr(
        "omnigent.server.google_cloud_approval.request_google_cloud_access", attach
    )
    command = asyncio.create_task(
        preflight_google_cloud(request_for(setup), setup.host, {"command": "terraform plan"})
    )
    await asyncio.wait_for(attached.wait(), 2)
    cancel_google_cloud_access(setup.session)
    assert await asyncio.wait_for(command, 2) is not None
    with pytest.raises(OmnigentError):
        await _resolve_elicitation(
            setup.session,
            {"elicitation_id": pending.id, "action": "accept"},
            None,
            approval_request=request_for(setup),
            approval_auth=UserAuth(),
        )
    assert setup.store.access.host(setup.host, OWNER) != "allowed"


@pytest.mark.asyncio
async def test_malformed_and_cross_session_verdicts_do_not_settle_prompt(setup):
    pending = await request_google_cloud_access(setup.store, setup.host)
    for session_id, action in [("wrong-session", "accept"), (setup.session, "allow")]:
        with pytest.raises(OmnigentError):
            await _resolve_elicitation(
                session_id,
                {"elicitation_id": pending.id, "action": action},
                None,
                approval_request=request_for(setup),
                approval_auth=UserAuth(),
            )
    assert not pending.future.done()


@pytest.mark.asyncio
async def test_settings_decision_resumes_native_waiter(setup):
    from omnigent.server.google_cloud_approval import settle_google_cloud_access

    pending = await request_google_cloud_access(setup.store, setup.host)
    setup.store.access.session(
        setup.session, OWNER, decision="allowed", generation=pending.context["generation"]
    )
    settle_google_cloud_access(setup.session, OWNER, pending.context["generation"], "allowed")
    assert await pending.future
    assert pending_elicitations.count_for(setup.session) == 0


@pytest.mark.asyncio
async def test_actual_timeout_clears_native_prompt(setup, monkeypatch):
    monkeypatch.setattr("omnigent.server.google_cloud_approval._TIMEOUT", 0.01)
    pending = await request_google_cloud_access(setup.store, setup.host)
    assert await asyncio.wait_for(asyncio.shield(pending.future), 2) is False
    assert pending_elicitations.count_for(setup.session) == 0
    assert setup.store.access.host(setup.host, OWNER) != "allowed"


@pytest.mark.asyncio
async def test_custom_provider_must_explicitly_authorize_credential_grants(setup):
    from omnigent.server.auth import AuthProvider

    class DelegatingProvider(AuthProvider):
        def get_user_id(self, request):
            return OWNER

    provider = DelegatingProvider()
    request = request_for(setup)
    assert provider.get_user_id(request) == OWNER
    assert provider.get_credential_user_id(request) is None
    pending = await request_google_cloud_access(setup.store, setup.host)
    with pytest.raises(OmnigentError):
        await _resolve_elicitation(
            setup.session,
            {"elicitation_id": pending.id, "action": "accept"},
            None,
            approval_request=request,
            approval_auth=provider,
        )
    assert not pending.future.done()
    assert setup.store.access.host(setup.host, OWNER) == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize("condition", ["shared", "deleted", "missing", "wrong_owner"])
async def test_unresolvable_sandbox_denies_cloud_command(setup, condition):
    import uuid

    from omnigent.db.db_models import SqlHost

    host_id = setup.host
    with Session(get_or_create_engine(setup.uri)) as db:
        host = db.get(SqlHost, (0, host_id))
        if condition == "shared":
            db.add(
                SqlConversationMetadata(
                    id=uuid.uuid4().hex, host_id=host_id, workspace="/other", kind=1
                )
            )
        elif condition == "deleted":
            host.deleted_at = int(time.time())
        elif condition == "wrong_owner":
            host.user_id = "other@example.com"
        else:
            host_id = uuid.uuid4().hex
        db.commit()
    assert (
        await preflight_google_cloud(
            request_for(setup), host_id, {"command": "gcloud projects list"}
        )
        is not None
    )
    assert pending_elicitations.count_for(setup.session) == 0


@pytest.mark.asyncio
async def test_explicitly_unconnected_account_can_use_external_cli_credentials(setup):
    from omnigent.db.db_models import SqlConnection

    with Session(get_or_create_engine(setup.uri)) as db:
        db.delete(db.get(SqlConnection, (0, OWNER, "google_cloud", "")))
        db.commit()
    assert (
        await preflight_google_cloud(
            request_for(setup), setup.host, {"command": "gcloud projects list"}
        )
        is None
    )
    assert pending_elicitations.count_for(setup.session) == 0


@pytest.mark.asyncio
async def test_cancelled_waiter_leaves_shared_approval_available(setup, monkeypatch):
    pending = await request_google_cloud_access(setup.store, setup.host)
    attached = asyncio.Event()
    callers = 0

    async def attach(store, host_id):
        nonlocal callers
        callers += 1
        if callers == 2:
            attached.set()
        return pending

    monkeypatch.setattr(
        "omnigent.server.google_cloud_approval.request_google_cloud_access", attach
    )
    tasks = [
        asyncio.create_task(
            preflight_google_cloud(
                request_for(setup), setup.host, {"command": "gcloud projects list"}
            )
        )
        for _ in range(2)
    ]
    try:
        await asyncio.wait_for(attached.wait(), 2)
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert not pending.future.done()
        assert not tasks[1].done()
        assert pending_elicitations.count_for(setup.session) == 1
        await _resolve_elicitation(
            setup.session,
            {"elicitation_id": pending.id, "action": "accept"},
            None,
            approval_request=request_for(setup),
            approval_auth=UserAuth(),
        )
        assert await asyncio.wait_for(tasks[1], 2) is None
        assert setup.store.access.host(setup.host, OWNER) == "allowed"
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
