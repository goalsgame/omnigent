import json
import sys
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from fastapi import FastAPI

from omnigent.server.inference_proxy import InferenceProxy, InferenceProxyConfig


@pytest.fixture
def setup():
    conv = SimpleNamespace(id="session", host_id="host", runner_id="runner", archived=False)
    host = SimpleNamespace(
        user_id="alice",
        account_generation="generation",
        status="online",
        updated_at=int(time.time()),
    )
    conversations = SimpleNamespace(
        get_conversation=lambda sid: conv if sid == conv.id else None,
        get_session_owner_authority=lambda sid, owner_only: SimpleNamespace(
            user_id="alice", generation="generation"
        ),
    )
    hosts = SimpleNamespace(get_host=lambda hid: host)
    calls = []

    async def credential():
        return {"token": "upstream-secret"}

    def upstream(request):
        calls.append(request)
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=b'data: {"text":"hello"}\n\ndata: [DONE]\n\n',
        )

    service = InferenceProxy(
        InferenceProxyConfig("https://server.example", "k" * 32, ("model-a",)),
        conversations,
        hosts,
        SimpleNamespace(credential=credential),
        transport=httpx.MockTransport(upstream),
    )
    app = FastAPI()
    app.include_router(service.router(), prefix="/v1")
    return service, app, conv, host, calls


async def access(client, service, conv):
    owner = service.conversations.get_session_owner_authority(conv.id, owner_only=True)
    grant = service.launch_config(
        conv, conv.runner_id, "openai-agents", conv.host_id, owner.user_id, owner.generation
    )["server_inference"]["grant"]
    response = await client.post(
        "/v1/inference/sessions/session/token", headers={"Authorization": "Bearer " + grant}
    )
    assert response.status_code == 200
    return response.json()["token"]


@pytest.mark.asyncio
async def test_stream_auth_and_no_credential_leak(setup):
    service, app, conv, _host, calls = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            "/v1/inference/sessions/session/chat/completions",
            json={"model": "model-a", "stream": True},
            headers={"Authorization": "Bearer " + token, "X-Leak": "client-secret"},
        )
        assert response.status_code == 200
        assert response.content.endswith(b"data: [DONE]\n\n")
        assert "upstream-secret" not in response.text
        assert calls[0].headers["authorization"] == "Bearer upstream-secret"
        assert "x-leak" not in calls[0].headers
        assert service.active_requests == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["runner", "host", "owner", "archived", "deleted", "offline"])
async def test_revocation_checks_current_state(setup, mutation):
    service, app, conv, host, calls = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        if mutation == "runner":
            conv.runner_id = "replacement"
        if mutation == "host":
            conv.host_id = "replacement"
        if mutation == "owner":
            host.user_id = "bob"
        if mutation == "archived":
            conv.archived = True
        if mutation == "deleted":
            service.conversations.get_conversation = lambda _: None
        if mutation == "offline":
            host.status = "offline"
        response = await client.post(
            "/v1/inference/sessions/session/responses",
            json={"model": "model-a"},
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 401
        assert not calls


@pytest.mark.asyncio
async def test_models_expiry_and_token_separation(setup):
    service, app, conv, _host, calls = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        for payload in ({"model": "not-allowed"}, {"model": "model-a", "models": ["not-allowed"]}):
            response = await client.post(
                "/v1/inference/sessions/session/responses",
                json=payload,
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 403
        assert (
            await client.post(
                "/v1/inference/sessions/session/token",
                headers={"Authorization": "Bearer " + token},
            )
        ).status_code == 401
        claims = jwt.decode(
            token,
            service.config.signing_key,
            algorithms=["HS256"],
            audience="omnigent-session-inference",
        )
        claims["exp"] = int(time.time()) - 1
        expired = jwt.encode(claims, service.config.signing_key, algorithm="HS256")
        response = await client.post(
            "/v1/inference/sessions/session/responses",
            json={"model": "model-a"},
            headers={"Authorization": "Bearer " + expired},
        )
        assert response.status_code == 401
        assert not calls


def test_host_configuration_keeps_grant_out_of_provider_config(tmp_path):
    from omnigent.host.inference_proxy import prepare
    from omnigent.inference_config import resolve_bound_provider

    config = prepare(
        {
            "server_inference": {
                "url": "https://server.example/v1/inference/sessions/session",
                "grant": "g" * 64,
                "models": ["model-a"],
                "harness": "openai-agents",
            }
        },
        tmp_path,
    )
    provider = resolve_bound_provider(config, "openai-agents", None)
    assert provider is not None
    assert "g" * 64 not in str(config)
    assert "auth_command" in str(config)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["provider", "redirect", "transport", "broker"])
async def test_provider_failures_release_capacity_and_hide_secrets(setup, failure):
    service, app, conv, _, _ = setup

    async def upstream(request):
        if failure == "transport":
            raise httpx.ReadTimeout("secret diagnostic")
        return httpx.Response(
            302 if failure == "redirect" else 401,
            headers={"location": "https://untrusted.example"},
            content=b"upstream-secret",
        )

    service.transport = httpx.MockTransport(upstream)
    if failure == "broker":

        async def unavailable():
            raise RuntimeError("upstream-secret")

        service.broker.credential = unavailable
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            "/v1/inference/sessions/session/responses",
            json={"model": "model-a"},
            headers={"Authorization": "Bearer " + token},
        )
    assert response.status_code == 502
    assert "secret" not in response.text
    assert "location" not in response.headers
    assert service.active_requests == 0
    assert not service.active_sessions


@pytest.mark.asyncio
async def test_request_limits_and_endpoint_allowlist(setup):
    from dataclasses import replace

    service, app, conv, _, calls = setup
    service.config = replace(service.config, max_request_bytes=64)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        headers = {"Authorization": "Bearer " + token}
        response = await client.post(
            "/v1/inference/sessions/session/responses",
            json={"model": "model-a", "input": "x" * 64},
            headers=headers,
        )
        assert response.status_code == 413
        for endpoint in ("keys", "responses?model=other", "chat/completions/extra"):
            response = await client.post(
                "/v1/inference/sessions/session/" + endpoint,
                json={"model": "model-a"},
                headers=headers,
            )
            assert response.status_code == 404
        response = await client.post(
            "/v1/inference/sessions/session/responses",
            content=b"{}",
            headers={**headers, "content-encoding": "gzip"},
        )
        assert response.status_code == 415
    assert not calls


@pytest.mark.asyncio
async def test_account_recreation_and_machine_revocation(setup):
    service, app, conv, host, calls = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        host.account_generation = "recreated"
        response = await client.get(
            "/v1/inference/sessions/session/models", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 401
        host.account_generation = None
        host.user_id = "oidc-machine:test"
        service.conversations.get_session_owner_authority = lambda *args, **kwargs: (
            SimpleNamespace(user_id=host.user_id, generation=None)
        )
        service.machine_allowed = lambda owner: True
        token = await access(client, service, conv)
        service.machine_allowed = lambda owner: False
        response = await client.get(
            "/v1/inference/sessions/session/models", headers={"Authorization": "Bearer " + token}
        )
        assert response.status_code == 401
    assert not calls


def test_config_requires_explicit_models_and_strong_signing_key(monkeypatch):
    monkeypatch.setenv("TEST_INFERENCE_KEY", "k" * 32)
    raw = {
        "public_url": "https://server.example",
        "signing_key_env": "TEST_INFERENCE_KEY",
        "models": ["model-a"],
    }
    config = InferenceProxyConfig.parse(raw)
    assert config.signing_key != "k" * 32
    assert InferenceProxyConfig.parse(None) is None
    for patch in (
        {"models": []},
        {"public_url": "http://server.example"},
        {"public_url": "https://:"},
        {"public_url": "https://server example.com"},
        {"public_url": "https://exa%mple.com"},
        {"public_url": "https://server.example:notaport"},
        {"public_url": "https://server.example:65536"},
        {"public_url": "https://server.example:0"},
        {"public_url": "https://user:password@server.example"},
        {"max_concurrent_requests": 0},
        {"signing_key_env": "MISSING_KEY"},
        {"unknown": True},
    ):
        with pytest.raises(ValueError):
            InferenceProxyConfig.parse({**raw, **patch})


def test_host_opt_in_and_transfer_use_destination_host(setup):
    from omnigent.server.host_registry import HostRegistry

    service, _, conv, _, _ = setup

    class Registry:
        server_inference_harnesses = ("openai-agents",)
        inference_config_resolver = service.launch_config
        needs_server_inference = HostRegistry.needs_server_inference
        server_inference_eligible = HostRegistry.server_inference_eligible
        get = staticmethod(
            lambda host: (
                SimpleNamespace(
                    hello=SimpleNamespace(capabilities=["server-inference-v1"]),
                    reported_harnesses={"openai-agents": "needs-auth"},
                    owner="alice",
                    account_generation="generation",
                )
                if host == "destination"
                else None
            )
        )

    registry = Registry()
    conv.inference_snapshot = {"runtime_config": {"direct": True}}
    assert HostRegistry.runner_inference_config(registry, conv, "new-runner", "openai-agents") == {
        "direct": True
    }
    assert HostRegistry.runner_inference_config(
        registry, conv, "new-runner", "openai-agents", host_id="destination"
    ) == {"direct": True}
    conv.inference_snapshot = None
    assert (
        HostRegistry.runner_inference_config(
            registry,
            conv,
            "new-runner",
            "openai-agents",
            host_id="destination",
            agent_has_auth=True,
        )
        is None
    )
    config = HostRegistry.runner_inference_config(
        registry, conv, "new-runner", "openai-agents", host_id="destination"
    )
    assert config["server_inference"]["grant"] == service._grant(
        conv.id, "new-runner", "destination", "alice", "generation"
    )
    reported = {"openai-agents": "needs-auth", "pi-native": "needs-cli"}
    assert HostRegistry.inference_readiness(registry, [], reported) == reported
    assert HostRegistry.inference_readiness(registry, ["server-inference-v1"], reported) == {
        "openai-agents": True,
        "pi-native": "needs-cli",
    }
    assert reported["openai-agents"] == "needs-auth"


@pytest.mark.asyncio
async def test_cancellation_closes_upstream_and_releases_capacity(setup):
    import asyncio

    service, app, conv, _, _ = setup
    started = asyncio.Event()
    closed = asyncio.Event()

    class EndlessStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: first\n\n"
            started.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    service.transport = httpx.MockTransport(
        lambda req: httpx.Response(200, stream=EndlessStream())
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)

        async def request():
            return await client.post(
                "/v1/inference/sessions/session/responses",
                json={"model": "model-a"},
                headers={"Authorization": "Bearer " + token},
            )

        pending = asyncio.create_task(request())
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert (await request()).status_code == 429
        finally:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
    assert closed.is_set()
    assert service.active_requests == 0
    assert not service.active_sessions


@pytest.mark.asyncio
async def test_responses_are_stateless(setup):
    service, app, conv, _, calls = setup
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        for extra in (
            {"previous_response_id": "another-session"},
            {"store": True},
            {"background": True},
            {"conversation": "another-session"},
        ):
            response = await client.post(
                "/v1/inference/sessions/session/responses",
                json={"model": "model-a", **extra},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 400
        response = await client.post(
            "/v1/inference/sessions/session/responses",
            json={"model": "model-a"},
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 200

    assert json.loads(calls[0].content)["store"] is False


def test_native_shell_auth_refreshes_without_posix_shell_dependency(monkeypatch):
    from omnigent.inner import openai_agents_sdk_executor as executor

    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"token-{len(seen)}\n")

    monkeypatch.setattr(executor.subprocess, "run", run)
    auth = executor._ShellCommandBearerAuth('"C:\\Program Files\\Python\\python.exe" -m helper')
    for index in (1, 2):
        request = httpx.Request("POST", "https://server.example/responses")
        assert next(auth.auth_flow(request)).headers["Authorization"] == f"Bearer token-{index}"
    assert seen[0][0].startswith('"C:')
    assert seen[0][1]["shell"] is True
    assert seen[0][1]["timeout"] == 30


def test_host_exchange_outputs_only_access_token(tmp_path, monkeypatch, capsys):

    from omnigent.host import inference_proxy

    path = tmp_path / "grant.json"
    path.write_text(
        json.dumps(
            {
                "url": "https://server.example/v1/inference/sessions/session",
                "grant": "narrow-grant",
            }
        )
    )
    monkeypatch.setattr(inference_proxy.sys, "argv", ["helper", str(path)])

    def post(url, **kwargs):
        assert url.endswith("/session/token")
        assert kwargs["headers"] == {"Authorization": "Bearer narrow-grant"}
        assert kwargs["follow_redirects"] is False
        return httpx.Response(
            200, json={"token": "access-token"}, request=httpx.Request("POST", url)
        )

    monkeypatch.setattr(inference_proxy.httpx, "post", post)
    assert inference_proxy.main() == 0
    assert capsys.readouterr().out == "access-token\n"
    path.write_text("invalid")
    assert inference_proxy.main() == 1
    output = capsys.readouterr()
    assert output.out == ""
    assert "narrow-grant" not in output.err


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["owner", "generation"])
async def test_old_grant_and_access_rejected_after_account_rebinding(setup, change):
    service, app, conv, host, _ = setup
    grant = service.launch_config(
        conv, conv.runner_id, "openai-agents", conv.host_id, "alice", "generation"
    )["server_inference"]["grant"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        if change == "owner":
            host.user_id = "bob"
        else:
            host.account_generation = "new-generation"
        service.conversations.get_session_owner_authority = lambda *args, **kwargs: (
            SimpleNamespace(user_id=host.user_id, generation=host.account_generation)
        )
        for credential, endpoint in ((grant, "token"), (token, "models")):
            method = client.post if endpoint == "token" else client.get
            response = await method(
                "/v1/inference/sessions/session/" + endpoint,
                headers={"Authorization": "Bearer " + credential},
            )
            assert response.status_code == 401


@pytest.mark.asyncio
async def test_sdk_auth_command_does_not_block_async_loop(monkeypatch):
    import asyncio
    import threading

    from omnigent.inner import openai_agents_sdk_executor as executor

    started = threading.Event()
    release = threading.Event()

    def run(*args, **kwargs):
        started.set()
        assert release.wait(5)
        return SimpleNamespace(returncode=0, stdout="refreshed-token")

    monkeypatch.setattr(executor.subprocess, "run", run)
    auth = executor._ShellCommandBearerAuth("auth-command")
    flow = auth.async_auth_flow(httpx.Request("POST", "https://server.example/responses"))
    pending = asyncio.create_task(anext(flow))
    try:
        await asyncio.wait_for(asyncio.to_thread(started.wait), 3)
        assert not pending.done()
    finally:
        release.set()
    assert (await pending).headers["Authorization"] == "Bearer refreshed-token"
    await flow.aclose()


@pytest.mark.skipif(
    sys.platform != "win32", reason="Executes the actual Windows cmd and PowerShell parsers"
)
def test_windows_cmd_executes_nested_powershell_command(tmp_path):
    import os

    from omnigent.inner.os_env import _shell_impl

    result = _shell_impl(
        command='powershell.exe -NoProfile -Command "Write-Output editor-host-ok"',
        timeout=10,
        shell_path=os.environ.get("COMSPEC", "cmd.exe"),
        cwd=tmp_path,
    )
    assert result["exit_code"] == 0
    assert result["stdout"].strip() == "editor-host-ok"


@pytest.mark.parametrize("readiness", [True, "needs-cli", None])
def test_personal_or_unavailable_harness_does_not_use_server_inference(readiness):
    from omnigent.server.host_registry import HostRegistry

    registry = HostRegistry()
    registry.server_inference_harnesses = ("openai-agents",)
    conn = SimpleNamespace(
        hello=SimpleNamespace(capabilities=["server-inference-v1"]),
        reported_harnesses={"openai-agents": readiness},
    )
    registry.get = lambda host: conn
    registry.inference_config_resolver = lambda *args: pytest.fail("unexpected server binding")
    conv = SimpleNamespace(host_id="host", inference_snapshot=None)
    assert registry.runner_inference_config(conv, "runner", "openai-agents") is None


@pytest.mark.asyncio
async def test_slow_upload_reserves_session_and_global_capacity(setup):
    import asyncio
    import copy
    from dataclasses import replace

    service, app, conv, _, calls = setup
    service.config = replace(service.config, max_concurrent_requests=1)
    other = copy.copy(conv)
    other.id = "other"
    service.conversations.get_conversation = lambda sid: {conv.id: conv, other.id: other}.get(sid)
    started = asyncio.Event()

    async def slow_body():
        yield b'{"model":'
        started.set()
        await asyncio.Event().wait()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        owner = service.conversations.get_session_owner_authority(other.id, owner_only=True)
        grant = service.launch_config(
            other, other.runner_id, "openai-agents", other.host_id, owner.user_id, owner.generation
        )["server_inference"]["grant"]
        response = await client.post(
            "/v1/inference/sessions/other/token", headers={"Authorization": "Bearer " + grant}
        )
        other_token = response.json()["token"]
        pending = asyncio.create_task(
            client.post(
                "/v1/inference/sessions/session/responses",
                content=slow_body(),
                headers={"Authorization": "Bearer " + token},
            )
        )
        try:
            await asyncio.wait_for(started.wait(), 5)
            assert service.active_requests == 1
            for sid, bearer in ((conv.id, token), (other.id, other_token)):
                blocked = await client.post(
                    f"/v1/inference/sessions/{sid}/responses",
                    json={"model": "model-a"},
                    headers={"Authorization": "Bearer " + bearer},
                )
                assert blocked.status_code == 429
        finally:
            pending.cancel()
            with pytest.raises(asyncio.CancelledError):
                await pending
        assert service.active_requests == 0
        assert not service.active_sessions
        for payload, expected in (
            (b"bad json", 400),
            (b'{"model":"other"}', 403),
            (b'{"model":"model-a","store":true}', 400),
        ):
            response = await client.post(
                "/v1/inference/sessions/session/responses",
                content=payload,
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == expected
            assert service.active_requests == 0
            assert not service.active_sessions
    assert not calls


def test_windows_refresh_command_keeps_metacharacters_out_of_cmd(monkeypatch):
    import base64

    from omnigent.host import inference_proxy

    monkeypatch.setattr(inference_proxy.sys, "platform", "win32")
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    monkeypatch.setattr(inference_proxy, "_system_powershell", lambda: powershell)
    args = [
        r"C:\Python & %PATH% (tools)!\python.exe",
        "-m",
        "omnigent.host.inference_proxy",
        r"C:\Omni & %TEMP% ! (data)'s\grant.json",
    ]
    command = inference_proxy._refresh_command(args)
    prefix = '"' + powershell + '" -NoLogo -NoProfile -NonInteractive -EncodedCommand '
    assert command.startswith(prefix)
    script = base64.b64decode(command.removeprefix(prefix)).decode("utf-16-le")
    assert script == (
        "$ErrorActionPreference='Stop'; & "
        + " ".join("'" + arg.replace("'", "''") + "'" for arg in args)
        + "; exit $LASTEXITCODE"
    )
    assert not any(character in command for character in "&|%!'()")


@pytest.mark.skipif(sys.platform != "win32", reason="Executes Windows cmd and PowerShell")
def test_windows_refresh_command_executes_literal_metacharacter_paths(tmp_path, monkeypatch):
    import subprocess

    from omnigent.host.inference_proxy import _refresh_command

    directory = tmp_path / "Omni & %PROXY_TEST_PATH% ! (data)'s"
    directory.mkdir()
    credential = directory / "grant.json"
    credential.write_text("fixture-token", encoding="utf-8")
    monkeypatch.setenv("PROXY_TEST_PATH", "unexpected-expansion")
    command = _refresh_command(
        [
            sys.executable,
            "-c",
            "import pathlib,sys; print(pathlib.Path(sys.argv[1]).read_text())",
            str(credential),
        ]
    )
    result = subprocess.run(command, shell=True, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "fixture-token"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["NaN", "Infinity", "-Infinity", "1e999", "nested"])
async def test_invalid_json_numbers_and_nesting_return_400_and_release_capacity(setup, invalid):
    service, app, conv, _, calls = setup
    depth = 10000
    value = ("[" * depth + "0" + "]" * depth) if invalid == "nested" else invalid
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            "/v1/inference/sessions/session/responses",
            content='{"model":"model-a","input":' + value + "}",
            headers={"Authorization": "Bearer " + token},
        )
        assert response.status_code == 400
        assert service.active_requests == 0
        assert not service.active_sessions
        assert not calls


@pytest.mark.asyncio
async def test_signed_workspace_route_bootstraps_refresh_and_preserves_isolation(setup, tmp_path):
    from omnigent.db.db_models import current_workspace_id, workspace_scope
    from omnigent.host.inference_proxy import prepare

    service, app, conv, host, calls = setup
    lookups = []

    def conversation(sid):
        lookups.append(("session", current_workspace_id()))
        return conv if current_workspace_id() in (42, 43) and sid == conv.id else None

    def get_host(hid):
        lookups.append(("host", current_workspace_id()))
        return host if current_workspace_id() in (42, 43) and hid == conv.host_id else None

    owner = service.conversations.get_session_owner_authority(conv.id, owner_only=True)

    def get_owner(sid, owner_only):
        lookups.append(("owner", current_workspace_id()))
        return owner if current_workspace_id() in (42, 43) else None

    service.conversations.get_conversation = conversation
    service.conversations.get_session_owner_authority = get_owner
    service.hosts.get_host = get_host
    with workspace_scope(42):
        proxy = service.launch_config(
            conv, conv.runner_id, "openai-agents", conv.host_id, owner.user_id, owner.generation
        )["server_inference"]
    with workspace_scope(43):
        other = service.launch_config(
            conv, conv.runner_id, "openai-agents", conv.host_id, owner.user_id, owner.generation
        )["server_inference"]
    assert current_workspace_id() == 0
    config = prepare({"server_inference": proxy}, tmp_path)
    assert config["providers"]["server-inference"]["openai"]["base_url"] == proxy["url"]
    path = proxy["url"].removeprefix("https://server.example")
    other_path = other["url"].removeprefix("https://server.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        refreshed = await client.post(
            path + "/token", headers={"Authorization": "Bearer " + proxy["grant"]}
        )
        assert refreshed.status_code == 200, refreshed.text
        token = refreshed.json()["token"]
        claims = jwt.decode(
            token,
            service.config.signing_key,
            algorithms=["HS256"],
            audience="omnigent-session-inference",
        )
        assert claims["workspace"] == 42
        headers = {"Authorization": "Bearer " + token}
        assert (await client.get(path + "/models", headers=headers)).status_code == 200
        response = await client.post(
            path + "/responses", headers=headers, json={"model": "model-a"}
        )
        assert response.status_code == 200
        assert calls
        assert all(workspace == 42 for _, workspace in lookups)
        assert current_workspace_id() == 0
        assert service.active_requests == 0
        assert not service.active_sessions
        count = len(calls)
        assert (
            await client.post(
                other_path + "/token", headers={"Authorization": "Bearer " + proxy["grant"]}
            )
        ).status_code == 401
        assert (
            await client.post(
                other_path + "/responses", headers=headers, json={"model": "model-a"}
            )
        ).status_code == 401
        assert len(calls) == count
        before = len(lookups)
        corrupt = path.replace("/workspaces/", "/workspaces/x", 1)
        assert (
            await client.post(
                corrupt + "/token", headers={"Authorization": "Bearer " + proxy["grant"]}
            )
        ).status_code == 401
        assert len(lookups) == before
        wrong_session = path.rsplit("/", 1)[0] + "/another-session"
        assert (
            await client.post(
                wrong_session + "/token", headers={"Authorization": "Bearer " + proxy["grant"]}
            )
        ).status_code == 401
        assert len(lookups) == before
        assert current_workspace_id() == 0


@pytest.mark.asyncio
async def test_proxy_reuses_client_closes_responses_and_does_not_retain_cookies(setup):
    service, app, conv, _, calls = setup
    closed_responses = []

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"data: done\n\n"

        async def aclose(self):
            closed_responses.append(True)

    class Transport(httpx.AsyncBaseTransport):
        closes = 0

        async def handle_async_request(self, request):
            calls.append(request)
            return httpx.Response(
                200, stream=Body(), headers={"set-cookie": "session=private; Path=/"}
            )

        async def aclose(self):
            self.closes += 1

    transport = Transport()
    service.transport = transport
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        pool = None
        for _ in range(2):
            response = await client.post(
                "/v1/inference/sessions/session/responses",
                json={"model": "model-a"},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200
            if pool is None:
                pool = service._client
            assert service._client is pool
            assert not pool.is_closed
            assert transport.closes == 0
            assert "cookie" not in calls[-1].headers
            assert service.active_requests == 0
    assert len(closed_responses) == 2
    await service.aclose()
    assert pool.is_closed
    assert transport.closes == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", [None, "unrelated", "runner", "owner", "archived", "cycle"])
@pytest.mark.parametrize("side_chat", [False, True])
async def test_attestation_accepts_only_actual_same_runner_ancestor(setup, mutation, side_chat):
    from omnigent.entities import Conversation

    service, app, binding, host, _ = setup
    binding.parent_conversation_id = "root"
    binding.root_conversation_id = "root"
    rows = {
        "root": Conversation(
            id="root", created_at=1, updated_at=1, agent_id="agent", root_conversation_id="root"
        ),
        binding.id: binding,
    }
    parent_id = binding.id
    for sid in ("child", "grandchild"):
        rows[sid] = Conversation(
            id=sid,
            created_at=1,
            updated_at=1,
            agent_id="agent",
            kind="sub_agent",
            root_conversation_id="root",
            parent_conversation_id=parent_id,
            runner_id=binding.runner_id,
        )
        parent_id = sid
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    if side_chat:
        rows["grandchild"].kind = "default"
        rows["grandchild"].parent_conversation_id = None
        rows["grandchild"].root_conversation_id = "grandchild"
        rows["grandchild"].labels = {SIDE_CHAT_LABEL_KEY: "1", SIDE_CHAT_SOURCE_LABEL_KEY: "child"}
    service.conversations.get_conversation = rows.get
    service.conversations.get_session_owner_authority = lambda sid, owner_only: SimpleNamespace(
        user_id="bob" if mutation == "owner" and sid == "grandchild" else "alice",
        generation="generation",
    )
    if mutation == "unrelated":
        if side_chat:
            rows["grandchild"].labels[SIDE_CHAT_SOURCE_LABEL_KEY] = "root"
        else:
            rows["grandchild"].parent_conversation_id = "root"
    if mutation == "runner":
        rows["grandchild"].runner_id = "different"
    if mutation == "archived":
        rows["child"].archived = True
    if mutation == "cycle":
        rows["child"].parent_conversation_id = "grandchild"
    config = service.launch_config(
        binding,
        binding.runner_id,
        "openai-agents",
        binding.host_id,
        "alice",
        host.account_generation,
    )["server_inference"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        response = await client.post(
            config["url"] + "/token",
            params={"runner_session_id": "grandchild"},
            headers={"Authorization": "Bearer " + config["grant"]},
        )
    if mutation is None:
        assert response.status_code == 200, response.text
        assert response.json()["runtime_binding"]["runner_session_id"] == "grandchild"
        assert response.json()["runtime_binding"]["session_id"] == binding.id
    else:
        assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_inherited_owner_binding_revokes_when_root_authority_changes(setup):
    service, app, binding, host, _ = setup
    binding.root_conversation_id = "root"
    root_owner = SimpleNamespace(user_id="alice", generation="generation")
    service.conversations.get_session_owner_authority = lambda sid, owner_only: (
        root_owner if sid == "root" else None
    )
    config = service.launch_config(
        binding,
        binding.runner_id,
        "openai-agents",
        binding.host_id,
        "alice",
        host.account_generation,
    )["server_inference"]
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        first = await client.post(
            config["url"] + "/token", headers={"Authorization": "Bearer " + config["grant"]}
        )
        assert first.status_code == 200, first.text
        root_owner.generation = "replacement-registration"
        revoked = await client.post(
            config["url"] + "/token", headers={"Authorization": "Bearer " + config["grant"]}
        )
        assert revoked.status_code == 401


@pytest.mark.asyncio
async def test_asgi_disconnect_cancels_upstream_header_wait_and_releases_capacity(setup):
    import asyncio

    from asgiref.testing import ApplicationCommunicator

    service, app, conv, _, _ = setup
    started, cancelled = asyncio.Event(), asyncio.Event()
    first = True

    class Provider(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            nonlocal first
            if first:
                first = False
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return httpx.Response(200, content=b"done")

    service.transport = Provider()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.0"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/v1/inference/sessions/session/responses",
            "raw_path": b"/v1/inference/sessions/session/responses",
            "query_string": b"",
            "root_path": "",
            "server": ("server.example", 443),
            "client": ("127.0.0.1", 1234),
            "headers": [
                (b"authorization", ("Bearer " + token).encode()),
                (b"content-type", b"application/json"),
            ],
        }
        comm = ApplicationCommunicator(app, scope)
        await comm.send_input(
            {
                "type": "http.request",
                "body": json.dumps({"model": "model-a"}).encode(),
                "more_body": False,
            }
        )
        try:
            await asyncio.wait_for(started.wait(), 2)
            assert service.active_requests == 1
            await comm.send_input({"type": "http.disconnect"})
            await asyncio.wait_for(cancelled.wait(), 2)
            await comm.wait(timeout=2)
            assert service.active_requests == 0
            assert not service.active_sessions
            response = await client.post(
                "/v1/inference/sessions/session/responses",
                json={"model": "model-a"},
                headers={"Authorization": "Bearer " + token},
            )
            assert response.status_code == 200, response.text
        finally:
            await comm.wait(timeout=2)


@pytest.mark.parametrize("shadow", ["omnigent.py", "omnigent/__init__.py", "httpx.py"])
def test_refresh_command_does_not_import_workspace_modules(tmp_path, shadow):
    import os
    import subprocess

    from omnigent.host.inference_proxy import runtime_config

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = tmp_path / "shadow-executed"
    module = workspace / shadow
    module.parent.mkdir(parents=True, exist_ok=True)
    module.write_text(
        "from pathlib import Path\nPath(" + repr(str(marker)) + ").write_text('executed')\n"
        "raise RuntimeError('workspace code ran during authentication')\n"
    )
    credential = tmp_path / "grant.json"
    # Fail before networking, after the legitimate helper and its dependencies import.
    credential.write_text(json.dumps({"url": "http://invalid", "grant": "g" * 64}))
    command = runtime_config(
        {
            "url": "https://server.example/v1/inference/sessions/session",
            "harness": "openai-agents",
            "models": ["model-a"],
        },
        credential,
    )["providers"]["server-inference"]["openai"]["auth_command"]
    env = {k: v for k, v in os.environ.items() if k not in {"PYTHONPATH", "PYTHONSAFEPATH"}}
    result = subprocess.run(
        command,
        shell=True,
        cwd=workspace,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert not marker.exists(), result.stderr
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.strip() == "Session inference authentication unavailable"


@pytest.mark.parametrize("directory", [r"C:\Windows\System32", r"C:\Windows (system)\System32"])
def test_powershell_path_uses_windows_system_directory(monkeypatch, directory):
    import ctypes
    from pathlib import PureWindowsPath

    from omnigent.host.inference_proxy import _system_powershell

    monkeypatch.setattr(sys, "platform", "win32")

    def system_directory(buffer, size):
        buffer.value = directory
        return len(directory)

    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(GetSystemDirectoryW=system_directory)),
        raising=False,
    )
    monkeypatch.setenv("SystemRoot", r"C:\untrusted-workspace")
    expected = PureWindowsPath(directory) / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    assert _system_powershell() == str(expected)


@pytest.mark.parametrize("directory", ["relative", r"C:\%SHADOW%", r"C:\!SHADOW!", 'C:"shadow'])
def test_powershell_path_rejects_unsafe_system_directory(monkeypatch, directory):
    import ctypes

    from omnigent.host.inference_proxy import _system_powershell

    monkeypatch.setattr(sys, "platform", "win32")

    def system_directory(buffer, size):
        buffer.value = directory
        return len(directory)

    monkeypatch.setattr(
        ctypes,
        "windll",
        SimpleNamespace(kernel32=SimpleNamespace(GetSystemDirectoryW=system_directory)),
        raising=False,
    )
    with pytest.raises(ValueError, match="Unsafe Windows system directory"):
        _system_powershell()


@pytest.mark.skipif(sys.platform != "win32", reason="Executes native Windows executable lookup")
def test_windows_refresh_ignores_workspace_powershell_executable(tmp_path):
    import shutil
    import subprocess

    from omnigent.host.inference_proxy import _refresh_command

    shutil.copy2(sys.executable, tmp_path / "powershell.exe")
    command = _refresh_command([sys.executable, "-P", "-c", "print('trusted-system-shell')"])
    result = subprocess.run(
        command, shell=True, cwd=tmp_path, capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "trusted-system-shell"


def _multi_upstream_raw():
    return {
        "public_url": "https://server.example",
        "signing_key_env": "INFERENCE_TEST_SIGNING",
        "models": ["model-a", "claude-alias", "gemini-alias", "external-alias"],
        "harnesses": ["pi-native", "openai-agents"],
        "upstreams": {
            "router": {"kind": "openrouter"},
            "claude": {"kind": "vertex-anthropic", "project": "example-project", "location": "eu"},
            "gemini": {
                "kind": "vertex-openai",
                "project": "example-project",
                "location": "europe-west1",
            },
            "external": {
                "kind": "openai",
                "base_url": "https://inference.example/v1",
                "api_key_env": "INFERENCE_TEST_EXTERNAL",
            },
        },
        "model_routes": {
            "model-a": {"upstream": "router", "model": "openai/model-a"},
            "claude-alias": {"upstream": "claude", "model": "claude-test@20990101"},
            "gemini-alias": {"upstream": "gemini", "model": "google/gemini-test"},
            "external-alias": {"upstream": "external", "model": "private/model"},
        },
        "model_limits": {"claude-alias": {"context_window": 1000000, "max_output_tokens": 64000}},
    }


@pytest.fixture
def routed(setup, monkeypatch):
    monkeypatch.setenv("INFERENCE_TEST_SIGNING", "s" * 48)
    monkeypatch.setenv("INFERENCE_TEST_EXTERNAL", "external-secret")
    service, app, conv, host, calls = setup
    service.config = InferenceProxyConfig.parse(_multi_upstream_raw())

    async def credential():
        return "google-secret"

    service.google_credential = SimpleNamespace(credential=credential)
    return service, app, conv, host, calls


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "endpoint", "target", "credential"),
    [
        (
            "model-a",
            "chat/completions",
            "https://openrouter.ai/api/v1/chat/completions",
            "upstream-secret",
        ),
        (
            "gemini-alias",
            "chat/completions",
            "https://europe-west1-aiplatform.googleapis.com/v1/projects/example-project/locations/europe-west1/endpoints/openapi/chat/completions",
            "google-secret",
        ),
        (
            "external-alias",
            "responses",
            "https://inference.example/v1/responses",
            "external-secret",
        ),
        (
            "claude-alias",
            "v1/messages",
            "https://aiplatform.eu.rep.googleapis.com/v1/projects/example-project/locations/eu/publishers/anthropic/models/claude-test@20990101:streamRawPredict",
            "google-secret",
        ),
    ],
)
async def test_routes_select_only_operator_upstream_and_credential(
    routed, model, endpoint, target, credential
):
    service, app, conv, _, calls = routed
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            f"/v1/inference/sessions/session/{endpoint}",
            headers={"Authorization": "Bearer " + token, "X-Api-Key": "caller-secret"},
            json={
                "model": model,
                "stream": True,
                "messages": [{"role": "user", "content": "hello"}],
            },
        )
    assert response.status_code == 200
    assert str(calls[0].url) == target
    assert calls[0].headers["authorization"] == "Bearer " + credential
    assert "x-api-key" not in calls[0].headers
    assert response.content == b'data: {"text":"hello"}\n\ndata: [DONE]\n\n'
    assert all(
        secret not in response.text
        for secret in ("google-secret", "external-secret", "upstream-secret")
    )


@pytest.mark.asyncio
async def test_native_claude_preserves_tools_images_and_thinking(routed):
    service, app, conv, _, calls = routed
    payload = {
        "model": "claude-alias",
        "max_tokens": 256,
        "stream": False,
        "system": [{"type": "text", "text": "help", "cache_control": {"type": "ephemeral"}}],
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {"type": "base64", "media_type": "image/png", "data": "example"},
                    },
                    {"type": "tool_result", "tool_use_id": "call", "content": "ok"},
                ],
            }
        ],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": "high"},
        "tools": [{"name": "lookup", "description": "find", "input_schema": {"type": "object"}}],
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            "/v1/inference/sessions/session/v1/messages?beta=true",
            headers={
                "Authorization": "Bearer " + token,
                "anthropic-beta": "fine-grained-tool-streaming-2025-05-14",
            },
            json=payload,
        )
    assert response.status_code == 200
    outgoing = json.loads(calls[0].content)
    assert outgoing == {key: value for key, value in payload.items() if key != "model"} | {
        "anthropic_version": "vertex-2023-10-16"
    }
    assert str(calls[0].url).endswith(":rawPredict")
    assert calls[0].headers["anthropic-beta"] == "fine-grained-tool-streaming-2025-05-14"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "endpoint"),
    [
        ("claude-alias", "chat/completions"),
        ("model-a", "v1/messages"),
        ("gemini-alias", "responses"),
    ],
)
async def test_incompatible_protocol_fails_before_any_upstream_auth(routed, model, endpoint):
    service, app, conv, _, calls = routed

    async def forbidden():
        pytest.fail("invalid protocol must not obtain upstream credentials")

    service.broker = SimpleNamespace(credential=forbidden)
    service.google_credential = SimpleNamespace(credential=forbidden)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            f"/v1/inference/sessions/session/{endpoint}",
            headers={"Authorization": "Bearer " + token},
            json={"model": model},
        )
    assert response.status_code == 400
    assert not calls
    assert service.active_requests == 0


@pytest.mark.asyncio
async def test_google_refresh_failure_is_sanitized_and_releases_capacity(routed):
    service, app, conv, _, calls = routed

    async def unavailable():
        raise ValueError("secret-value-from-authentication-library")

    service.google_credential = SimpleNamespace(credential=unavailable)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            "/v1/inference/sessions/session/chat/completions",
            headers={"Authorization": "Bearer " + token},
            json={"model": "gemini-alias"},
        )
    assert response.status_code == 502
    assert "secret-value" not in response.text
    assert not calls and service.active_requests == 0


@pytest.mark.asyncio
async def test_owner_is_rechecked_after_google_refresh(routed):
    service, app, conv, host, calls = routed

    async def revoked():
        host.user_id = "other-owner"
        return "google-secret"

    service.google_credential = SimpleNamespace(credential=revoked)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        token = await access(client, service, conv)
        response = await client.post(
            "/v1/inference/sessions/session/chat/completions",
            headers={"Authorization": "Bearer " + token},
            json={"model": "gemini-alias"},
        )
    assert response.status_code == 401
    assert not calls and service.active_requests == 0


def test_native_model_protocols_and_limits_reach_real_pi_config(routed, tmp_path):
    from omnigent.harnesses.pi_native.credentials import resolve_pi_native_provider
    from omnigent.host.inference_proxy import prepare

    service, _, conv, _, _ = routed
    config = service.launch_config(
        conv, conv.runner_id, "pi-native", conv.host_id, "alice", "generation"
    )
    runtime = prepare(config, tmp_path)
    resolved = resolve_pi_native_provider(model="claude-alias", config_loader=lambda: runtime)
    assert resolved is not None
    assert resolved.api == "anthropic-messages"
    assert resolved.auth_header is True
    rendered = resolved.to_models_config()["providers"]
    protocols = {
        row["id"]: provider["api"] for provider in rendered.values() for row in provider["models"]
    }
    assert protocols == {
        "model-a": "openai-completions",
        "claude-alias": "anthropic-messages",
        "gemini-alias": "openai-completions",
        "external-alias": "openai-completions",
    }
    claude = next(
        row
        for provider in rendered.values()
        for row in provider["models"]
        if row["id"] == "claude-alias"
    )
    assert claude["contextWindow"] == 1000000
    assert claude["maxTokens"] == 64000
    assert all(
        provider["baseUrl"].startswith("https://server.example/v1/inference/")
        for provider in rendered.values()
    )
    assert "google-secret" not in str(runtime) and "external-secret" not in str(runtime)


@pytest.mark.asyncio
async def test_protocol_and_limit_metadata_is_signed_and_attested(routed):
    service, app, conv, _, _ = routed
    config = service.launch_config(
        conv, conv.runner_id, "pi-native", conv.host_id, "alice", "generation"
    )["server_inference"]
    path = config["url"].removeprefix("https://server.example")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="https://server.example"
    ) as client:
        response = await client.post(
            path + "/token", headers={"Authorization": "Bearer " + config["grant"]}
        )
    assert response.status_code == 200
    attested = response.json()["runtime_binding"]
    assert attested["model_apis"] == config["model_apis"]
    assert attested["model_limits"] == config["model_limits"]
    openai_config = service.launch_config(
        conv, conv.runner_id, "openai-agents", conv.host_id, "alice", "generation"
    )["server_inference"]
    assert "claude-alias" not in openai_config["models"]
    assert "model_apis" not in openai_config and "model_limits" not in openai_config
