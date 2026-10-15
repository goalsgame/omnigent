"""A saved inference profile cannot be silently reassigned to another runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from omnigent.entities import Conversation
from omnigent.runner import create_runner_app
from omnigent.runner.session_init_protocol import build_runner_session_init_payload
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", ["same", "different", "unconfigured-session", "unconfigured-runner"]
)
async def test_session_init_requires_the_runners_saved_profile(
    mode: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = {
        "providers": {"gateway": {"kind": "gateway"}},
        "inference": {"harnesses": {"codex": {"provider": "gateway"}}},
    }
    runner_config = config if mode != "unconfigured-runner" else {}
    expected = config if mode != "unconfigured-session" else None
    if mode == "different":
        runner_config = {"providers": {"other": {"kind": "gateway"}}, "inference": {}}
    config_path = tmp_path / "inference.json"
    config_path.write_text(json.dumps(runner_config))
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(config_path))
    conversation = Conversation(
        id="inference-session",
        created_at=1,
        updated_at=1,
        agent_id="inference-agent",
        root_conversation_id="inference-session",
        inference_snapshot={"runtime_config": expected} if expected is not None else None,
    )
    payload = build_runner_session_init_payload(conversation, server_version="0.6.0")
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        response = await client.post("/v1/sessions", json=payload)
    if mode == "same":
        assert response.status_code == 201, response.text
        assert response.json()["inference_config_verified"] is True
        assert len(manager.get_client_calls) == 1
    else:
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "inference_config_mismatch"
        assert manager.get_client_calls == []


@pytest.mark.asyncio
async def test_bound_runner_rejects_legacy_init_without_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / "inference.json"
    config_path.write_text(json.dumps({"providers": {"gateway": {"kind": "gateway"}}}))
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(config_path))
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions", json={"session_id": "legacy-session", "agent_id": "agent"}
        )
    assert response.status_code == 409, response.text
    assert manager.get_client_calls == []


@pytest.mark.asyncio
async def test_inference_mismatch_response_does_not_expose_exception_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from omnigent.runner.session_init_protocol import RunnerInferenceConfigMismatch

    monkeypatch.setattr(
        "omnigent.runner.session_init_protocol.validate_runner_inference_config",
        Mock(side_effect=RunnerInferenceConfigMismatch("private-path/credential-sentinel")),
    )
    conversation = Conversation(
        id="inference-session",
        created_at=1,
        updated_at=1,
        agent_id="inference-agent",
        root_conversation_id="inference-session",
    )
    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=manager,  # type: ignore[arg-type]
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    async with _runner_client(app) as client:
        response = await client.post(
            "/v1/sessions",
            json=build_runner_session_init_payload(conversation, server_version="0.6.0"),
        )
    assert response.status_code == 409, response.text
    assert response.json() == {
        "error": "inference_config_mismatch",
        "detail": (
            "This runner has a different saved provider configuration; launch a new runner."
        ),
    }
    assert "credential-sentinel" not in response.text
    assert manager.get_client_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode",
    [
        "valid",
        "authenticated-client",
        "colocated-child",
        "wrong-runner",
        "wrong-session",
        "tampered-provider",
        "tampered-policy",
        "revoked",
        "saved-direct",
    ],
)
async def test_transient_proxy_init_is_server_verified_and_runner_bound(
    mode, tmp_path, monkeypatch
):
    import hashlib

    import httpx

    from omnigent.host.inference_proxy import prepare
    from omnigent.runner.identity import RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, token_bound_runner_id

    token = "transient-runner-binding"
    monkeypatch.setenv(RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR, token)
    url = "https://server.example/v1/inference/workspaces/signed-selector/sessions/proxy-session"
    proxy = {"url": url, "grant": "g" * 64, "harness": "openai-agents", "models": ["model-a"]}
    actual = prepare({"server_inference": proxy}, tmp_path / "grants")
    if mode == "tampered-provider":
        actual["providers"]["server-inference"]["openai"]["base_url"] = "https://other.example"
    config_path = tmp_path / "inference.json"
    config_path.write_text(json.dumps(actual))
    monkeypatch.setenv("OMNIGENT_INFERENCE_CONFIG", str(config_path))
    if mode == "tampered-policy":
        actual["server_inference_binding"]["models"] = ["other-model"]
        actual["inference"]["harnesses"]["openai-agents"].update(
            model_allowlist=["other-model"], default_model="other-model"
        )
        config_path.write_text(json.dumps(actual))
    sid = "other-session" if mode in ("wrong-session", "colocated-child") else "proxy-session"
    conversation = Conversation(
        id=sid, created_at=1, updated_at=1, agent_id="agent", root_conversation_id=sid
    )
    if mode == "colocated-child":
        conversation.parent_conversation_id = "proxy-session"
        conversation.root_conversation_id = "proxy-session"
    if mode == "saved-direct":
        conversation.inference_snapshot = {
            "runtime_config": {"providers": {"direct": {}}, "inference": {}}
        }
    binding = {
        "url": url,
        "session_id": "proxy-session",
        "runner_id": token_bound_runner_id(token),
        "harness": "openai-agents",
        "models": ["model-a"],
        "grant_hash": hashlib.sha256(b"g" * 64).hexdigest(),
        "runner_session_id": sid,
    }
    if mode == "wrong-runner":
        binding["runner_id"] = "other-runner"
    verification_calls = []

    class Server(NullServerClient):
        async def post(self, url, **kwargs):
            if mode == "authenticated-client":
                return await authenticated.post(url, **kwargs)
            verification_calls.append((url, kwargs))
            return httpx.Response(
                401 if mode in ("revoked", "wrong-session") else 200,
                json={"runtime_binding": binding},
                request=httpx.Request("POST", "https://server.example" + url),
            )

    class ControlPlaneAuth(httpx.Auth):
        def auth_flow(self, request):
            request.headers["Authorization"] = "Bearer control-plane-token"
            yield request

    def refresh(request):
        assert request.headers["Authorization"] == "Bearer " + "g" * 64
        verification_calls.append((request.url.path, {}))
        return httpx.Response(200, json={"runtime_binding": binding})

    manager = _FakeProcessManager(_ScriptedHarnessClient([]))
    async with httpx.AsyncClient(
        base_url="https://server.example",
        auth=ControlPlaneAuth(),
        transport=httpx.MockTransport(refresh),
    ) as authenticated:
        app = create_runner_app(
            process_manager=manager,
            server_client=Server(),
        )
        async with _runner_client(app) as client:
            response = await client.post(
                "/v1/sessions",
                json=build_runner_session_init_payload(conversation, server_version="test"),
            )
    if mode in ("valid", "colocated-child", "authenticated-client"):
        assert response.status_code == 201, response.text
        assert response.json()["inference_config_verified"] is True
        assert verification_calls[0][0].startswith("/v1/inference/workspaces/")
        assert len(manager.get_client_calls) == 1
    else:
        assert response.status_code == 409, response.text
        assert response.json()["error"] == "inference_config_mismatch"
        assert not manager.get_client_calls
    if mode in ("tampered-provider", "saved-direct"):
        assert not verification_calls
