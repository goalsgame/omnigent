"""Cloud commands use the real policy gate and both standard approval transports."""

import asyncio
import time
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.orm import Session

from omnigent.connections.google_cloud import GoogleCloudConnectionStore
from omnigent.db.db_models import SqlConversationMetadata, SqlHost, SqlSessionPermission
from omnigent.db.utils import get_or_create_engine
from omnigent.runtime import pending_elicitations
from omnigent.server.google_cloud import SCOPES
from omnigent.server.google_cloud_approval import cancel_google_cloud_access
from tests.server.helpers import create_test_agent
from tests.server.test_github_store import SecretBox

OWNER = "owner@example.com"
HEADERS = {"X-Forwarded-Email": OWNER}


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["events", "resolve"])
@pytest.mark.parametrize("gate", ["native", "relay"])
async def test_cloud_command_pauses_then_resumes_after_owner_approval(
    auth_app, auth_client, db_uri, transport, gate
):
    agent = await create_test_agent(auth_client, user=OWNER)
    session_id = agent["_session_id"]
    host_id = uuid.uuid4().hex
    with Session(get_or_create_engine(db_uri)) as db:
        db.add(
            SqlHost(
                host_id=host_id,
                user_id=OWNER,
                name="sandbox",
                status=1,
                created_at=1,
                updated_at=1,
                sandbox_provider="agent_sandbox",
            )
        )
        row = db.get(SqlConversationMetadata, (0, session_id))
        row.host_id = host_id
        row.workspace = "/workspace"
        db.add(
            SqlSessionPermission(
                user_id="collaborator@example.com", conversation_id=session_id, level=2
            )
        )
        db.commit()
    store = GoogleCloudConnectionStore(db_uri, SecretBox("test"))
    store.upsert(
        OWNER,
        tokens={
            "access_token": "fixture-access",
            "refresh_token": "fixture-refresh",
            "expires_at": time.time() + 3600,
            "scope": SCOPES,
        },
        identity={"subject": "fixture-user", "email": OWNER},
    )
    auth_app.state.google_cloud_store = store
    auth_app.state.google_cloud_client = SimpleNamespace(token=AsyncMock())
    if gate == "native":
        path = f"/v1/sessions/{session_id}/policies/evaluate"
        body = {
            "event": {
                "type": "PHASE_TOOL_CALL",
                "data": {"name": "bash", "arguments": {"command": "gcloud projects list"}},
            }
        }
    else:
        path = f"/v1/sessions/{session_id}/events"
        body = {
            "type": "function_call",
            "data": {
                "name": "sys_os_shell",
                "agent": "root",
                "call_id": "call_cloud",
                "arguments": '{"command":"gcloud projects list"}',
                "evaluate_policy": True,
            },
        }
    task = asyncio.create_task(auth_client.post(path, json=body, headers=HEADERS))
    try:
        async with asyncio.timeout(5):
            while not pending_elicitations.snapshot_for(session_id):
                if task.done():
                    result = task.result()
                    pytest.fail(
                        f"Gate returned without approval: {result.status_code} {result.text}"
                    )
                await asyncio.sleep(0.01)
        prompt = pending_elicitations.snapshot_for(session_id)[0]
        assert prompt["params"]["policy_name"] == "Google Cloud access"
        assert not task.done()
        eid = prompt["elicitation_id"]
        poll = await auth_client.get(
            f"/v1/sessions/{session_id}/elicitations/{eid}", headers=HEADERS
        )
        assert poll.status_code == 200
        assert poll.json()["status"] == "pending"
        if transport == "events":
            url = f"/v1/sessions/{session_id}/events"
            verdict = {"type": "approval", "data": {"elicitation_id": eid, "action": "accept"}}
        else:
            url = f"/v1/sessions/{session_id}/elicitations/{eid}/resolve"
            verdict = {"action": "accept"}
        refused = await auth_client.post(
            url, json=verdict, headers={"X-Forwarded-Email": "collaborator@example.com"}
        )
        assert refused.status_code == 403, refused.text
        assert not task.done()
        accepted = await auth_client.post(url, json=verdict, headers=HEADERS)
        assert accepted.status_code == 202, accepted.text
        result = await asyncio.wait_for(task, 5)
        assert result.status_code in (200, 202), result.text
        if gate == "native":
            assert result.json()["result"] in ("POLICY_ACTION_ALLOW", "POLICY_ACTION_UNSPECIFIED")
        else:
            assert result.json()["verdict"] == "allow"
        assert store.access.host(host_id, OWNER) == "allowed"
        assert pending_elicitations.count_for(session_id) == 0
    finally:
        cancel_google_cloud_access(session_id)
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
