"""Configured server login to managed-host stdio MCP, with a synthetic IdP/upstream."""

import asyncio
import dataclasses
import json
import socket
import sys
import time
import uuid
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
import uvicorn
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.conftest import mock_llm, runtime_init
from tests.server.test_credential_store import _FakeCipher
from tests.server.test_oidc_machine_auth import oidc, signing_key

__all__ = ["mock_llm", "oidc", "runtime_init", "signing_key"]


@pytest.mark.asyncio
@pytest.mark.parametrize("offline", [False, True])
async def test_login_owner_invokes_mcp_from_managed_stdio_host(
    db_uri, tmp_path, monkeypatch, runtime_init, oidc, signing_key, offline
):
    owner, subject = "person@example.test", "person-subject"
    host_id = uuid.uuid4().hex
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen()
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    oidc = dataclasses.replace(oidc, redirect_uri=base + "/auth/callback")
    if offline:
        oidc = dataclasses.replace(oidc, scopes=oidc.scopes + " offline_access")
        monkeypatch.setenv("OMNIGENT_OIDC_DELEGATION_TTL_HOURS", "168")

    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update(kid="fixture", alg="RS256", use="sig")
    monkeypatch.setattr(
        "omnigent.server.oidc_login._BoundedJWKClient.get_signing_key_from_jwt",
        lambda self, token: jwt.PyJWK.from_dict(jwk),
    )

    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda self: {"keys": [jwk]})
    monkeypatch.setattr(
        "omnigent.stores.credential_store.build_secret_cipher", lambda: _FakeCipher()
    )
    monkeypatch.setenv("TEST_MCP_CLIENT_SECRET", "fixture-secret")
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    hosts = HostStore(db_uri)
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifact_store=artifacts, cache_dir=tmp_path / "cache"),
        permission_store=SqlAlchemyPermissionStore(db_uri),
        host_store=hosts,
        auth_provider=UnifiedAuthProvider("oidc", oidc_config=oidc),
        server_config={
            "oidc_mcp": {
                "upstream_url": "https://tools.example.test/mcp",
                "audience": "tools-api",
                "scope": "openid tools-api",
                "machine_client_id": "machine-exchange",
                "machine_client_secret_env": "TEST_MCP_CLIENT_SECRET",
            }
        },
    )

    def signed(audience, kind=None):
        now = int(time.time())
        return jwt.encode(
            {
                "iss": oidc.issuer,
                "aud": audience,
                "sub": subject,
                "azp": oidc.client_id,
                "typ": kind or ("ID" if audience == oidc.client_id else "Bearer"),
                "iat": now,
                "exp": now + 300,
                "email": owner,
                "email_verified": True,
            },
            signing_key,
            algorithm="RS256",
            headers={"kid": "fixture"},
        )

    original_post = httpx.AsyncClient.post

    async def idp_login(client, url, **kwargs):
        if str(url) == oidc.token_endpoint:
            assert kwargs["data"]["grant_type"] == "authorization_code"
            return httpx.Response(
                200,
                json={
                    "id_token": signed(oidc.client_id),
                    "access_token": signed("login-api") if offline else "login-access",
                    "refresh_token": signed(oidc.client_id, "Offline"),
                    "expires_in": 300,
                },
            )
        return await original_post(client, url, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "post", idp_login)

    def exchange(request):
        form = parse_qs(request.content.decode())
        if not offline:
            assert form["subject_token"] == ["login-access"]
        if offline:
            assert form["requested_token_type"] == [
                "urn:ietf:params:oauth:token-type:access_token"
            ]
        assert form["client_id"] == [oidc.client_id]
        return httpx.Response(
            200,
            json={
                "access_token": signed("tools-api"),
                "refresh_token": "server-only-refresh",
                "expires_in": 300,
                "token_type": "Bearer",
            },
        )

    app.state.mcp_identity_broker.client.close()
    app.state.mcp_identity_broker.client = httpx.Client(transport=httpx.MockTransport(exchange))
    callers = []

    def upstream(request):
        claims = jwt.decode(
            request.headers["authorization"].removeprefix("Bearer "),
            signing_key.public_key(),
            algorithms=["RS256"],
            issuer=oidc.issuer,
            audience="tools-api",
        )
        callers.append(claims["sub"])
        assert "x-omnigent-host-token" not in request.headers
        assert "cookie" not in request.headers
        if request.method != "POST":
            return httpx.Response(405)
        message = json.loads(request.content)
        if "id" not in message:
            return httpx.Response(202)
        if message["method"] == "initialize":
            result = {
                "protocolVersion": message["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "identity-fixture", "version": "1"},
            }
        elif message["method"] == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "whoami",
                        "description": "Return verified owner",
                        "inputSchema": {"type": "object"},
                    }
                ]
            }
        else:
            assert message["method"] == "tools/call"
            result = {"content": [{"type": "text", "text": claims["sub"]}], "isError": False}
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": message["id"], "result": result})

    from omnigent.server.routes import mcp_identity

    monkeypatch.setattr(
        mcp_identity,
        "httpx",
        SimpleNamespace(
            AsyncClient=lambda **kwargs: httpx.AsyncClient(
                transport=httpx.MockTransport(upstream), **kwargs
            ),
            Timeout=httpx.Timeout,
            HTTPError=httpx.HTTPError,
        ),
    )
    server = uvicorn.Server(uvicorn.Config(app, log_level="error"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    try:
        async with asyncio.timeout(30):
            while not server.started:
                if task.done():
                    await task
                    pytest.fail("Server did not start")
                await asyncio.sleep(0.01)
            async with httpx.AsyncClient(base_url=base) as browser:
                login = await browser.get("/auth/login")
                state = parse_qs(urlsplit(login.headers["location"]).query)["state"][0]
                callback = await browser.get(
                    "/auth/callback", params={"state": state, "code": "fixture-code"}
                )
                assert callback.status_code == 302, callback.text
                assert callback.cookies.get("ap_session")
                if offline:
                    assert callback.cookies.get("ap_oidc_renewal")
            hosts.register_managed_host(
                host_id=host_id,
                name="fixture",
                user_id=owner,
                token="fixture-launch-token",
                provider="agent_sandbox",
                sandbox_id="fixture-sandbox",
                token_expires_at=int(time.time()) + 300,
            )
            hosts.upsert_on_connect(
                host_id, "fixture", owner, managed_token="fixture-launch-token"
            )
            coords = tmp_path / "host-credentials.json"
            coords.write_text(
                json.dumps(
                    {
                        "server": base,
                        "host_id": host_id,
                        "host_token": "fixture-launch-token",
                    }
                )
            )
            params = StdioServerParameters(
                command=sys.executable,
                args=[
                    "-c",
                    "import sys; from pathlib import Path; import anyio; "
                    "import omnigent.host.mcp_identity as bridge; "
                    "bridge._coords_path=lambda: Path(sys.argv[1]); anyio.run(bridge.main)",
                    str(coords),
                ],
            )
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                assert [tool.name for tool in (await session.list_tools()).tools] == ["whoami"]
                result = await session.call_tool("whoami", {})
                assert not result.isError
                assert result.content[0].text == subject
            assert callers and set(callers) == {subject}
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, timeout=15)
        sock.close()
