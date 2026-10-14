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
    import json

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
    import json

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
    args = [
        r"C:\Python & %PATH% (tools)!\python.exe",
        "-m",
        "omnigent.host.inference_proxy",
        r"C:\Omni & %TEMP% ! (data)'s\grant.json",
    ]
    command = inference_proxy._refresh_command(args)
    prefix = "powershell.exe -NoLogo -NoProfile -NonInteractive -EncodedCommand "
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
