"""Direct machine opt-in through the real HTTP broker and sandbox metadata adapter."""

import socket
import threading
import time
import uuid
from datetime import UTC, datetime

import httpx
import uvicorn
from fastapi import FastAPI
from sqlalchemy.orm import Session

from omnigent.db.db_models import SqlSessionPermission
from omnigent.db.utils import get_or_create_engine
from omnigent.host.google_cloud import GoogleCloudMetadataServer
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.google_cloud_machine import GoogleCloudMachineBroker, MachineCloudBinding
from omnigent.server.routes.google_cloud_machine import create_machine_cloud_router
from omnigent.server.routes.host_credentials import create_host_credentials_router
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.test_oidc_machine_auth import (
    PRINCIPAL,
    machine_config,
    oidc,
    signed_token,
    signing_key,
    verifier,
)

__all__ = ["machine_config", "oidc", "signing_key", "verifier"]


def test_machine_opt_in_to_metadata_token(db_uri, oidc, verifier, signing_key):
    sid, hid = uuid.uuid4().hex, uuid.uuid4().hex
    hosts = HostStore(db_uri)
    hosts.register_managed_host(
        host_id=hid,
        name="machine-workspace",
        user_id=PRINCIPAL,
        token="launch",
        provider="agent_sandbox",
        sandbox_id="test-sandbox",
        token_expires_at=int(time.time()) + 300,
    )
    conversations = SqlAlchemyConversationStore(db_uri)
    conversations.create_conversation(conversation_id=sid, host_id=hid, workspace="/workspace")
    permissions = SqlAlchemyPermissionStore(db_uri)
    with Session(get_or_create_engine(db_uri)) as db:
        db.add(SqlSessionPermission(user_id=PRINCIPAL, conversation_id=sid, level=4))
        db.commit()
    verifier.set_principal_check(lambda p: p == PRINCIPAL and not permissions.is_admin(p))
    auth = UnifiedAuthProvider("oidc", oidc_config=oidc, machine_verifier=verifier)
    calls = []

    def google(request):
        calls.append(request.url.host)
        if request.url.host == "metadata.google.internal":
            assert request.headers["Metadata-Flavor"] == "Google"
            return httpx.Response(200, json={"access_token": "server-source-token"})
        assert request.url.host == "iamcredentials.googleapis.com"
        assert request.headers["Authorization"] == "Bearer server-source-token"
        return httpx.Response(
            200,
            json={
                "accessToken": "machine-short-token",
                "expireTime": datetime.fromtimestamp(time.time() + 600, UTC).isoformat(),
            },
        )

    broker = GoogleCloudMachineBroker(
        db_uri,
        {PRINCIPAL: MachineCloudBinding("test-bot@example-project.iam.gserviceaccount.com")},
        verifier.principal_allowed,
        transport=httpx.MockTransport(google),
    )
    app = FastAPI()
    app.state.google_cloud_machine_broker = broker
    app.include_router(create_host_credentials_router(hosts), prefix="/v1")
    app.include_router(
        create_machine_cloud_router(broker, auth, permissions, conversations), prefix="/v1"
    )
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", lifespan="off"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        metadata = None
        metadata_thread = None
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.01)
            address = f"http://127.0.0.1:{sock.getsockname()[1]}"
            metadata = GoogleCloudMetadataServer(address, hid, "launch")
            metadata_thread = threading.Thread(target=metadata.serve_forever, daemon=True)
            metadata_thread.start()
            token_url = f"http://127.0.0.1:{metadata.server_port}/computeMetadata/v1/instance/service-accounts/default/token"
            with httpx.Client(trust_env=False, timeout=10) as client:

                def token_response():
                    return client.get(token_url, headers={"Metadata-Flavor": "Google"})

                assert token_response().status_code == 403
                assert calls == []
                decision = client.post(
                    f"{address}/v1/sessions/{sid}/google-cloud",
                    json={"enabled": True},
                    headers={"Authorization": "Bearer " + signed_token(signing_key)},
                )
                assert decision.status_code == 200, decision.text
                assert decision.json()["authorization"] == "operator"
                token = token_response()
                assert token.status_code == 200, token.text
                assert token.json()["access_token"] == "machine-short-token"
                assert token.json()["token_type"] == "Bearer"
                assert 0 < token.json()["expires_in"] <= 600
                assert token.headers["Cache-Control"] == "no-store"
                assert calls == ["metadata.google.internal", "iamcredentials.googleapis.com"]
                decision = client.post(
                    f"{address}/v1/sessions/{sid}/google-cloud",
                    json={"enabled": False},
                    headers={"Authorization": "Bearer " + signed_token(signing_key)},
                )
                assert decision.status_code == 200
                assert token_response().status_code == 403
        finally:
            if metadata is not None:
                metadata.shutdown()
                metadata.server_close()
            if metadata_thread is not None:
                metadata_thread.join(timeout=10)
                assert not metadata_thread.is_alive()
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
