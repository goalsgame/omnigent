"""Managed host -> credential broker -> real Pi request, with a local OAuth/model service.

Uses real stores, file-backed application configuration, HTTP, host coordinate
setup and credential-helper subprocesses. No cloud credentials are required.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs

import httpx
import pytest
import uvicorn
import yaml
from fastapi import Request
from fastapi.responses import StreamingResponse

from omnigent.harnesses.pi_native.credentials import (
    pi_native_provider_launch,
    resolve_pi_native_provider,
)
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server import openrouter_wif as wif
from omnigent.server.app import create_app
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore


@pytest.mark.timeout(120)
def test_managed_host_pi_uses_file_config_and_renews_workload_token(tmp_path, db_uri, monkeypatch):
    pi = shutil.which("pi")
    if pi is None:
        pytest.skip("Pi CLI required for the workload-identity harness journey")
    monkeypatch.setenv("HOME", str(tmp_path))
    identity = tmp_path / "identity.jwt"
    identity.write_text("first-identity")
    config_path = tmp_path / "server.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "openrouter_wif": {
                    "policy_id": "test-policy",
                    "subject_token_file": str(identity),
                }
            }
        )
    )
    monkeypatch.setenv("OMNIGENT_CONFIG", str(config_path))
    clock = [100.0]
    monkeypatch.setattr(wif, "time", type("Clock", (), {"monotonic": lambda: clock[0]}))
    host_id = "a" * 32
    hosts = HostStore(db_uri)
    hosts.register_managed_host(
        host_id=host_id,
        name="test-host",
        user_id="test@example.com",
        token="test-launch-token",
        provider="test",
        sandbox_id="test-sandbox",
        token_expires_at=int(time.time()) + 600,
    )
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    # Omitting server_config exercises hosted entrypoints' actual file-loading path.
    app = create_app(
        agent_store=SqlAlchemyAgentStore(db_uri),
        file_store=SqlAlchemyFileStore(db_uri),
        conversation_store=SqlAlchemyConversationStore(db_uri),
        artifact_store=artifacts,
        agent_cache=AgentCache(artifacts, tmp_path / "cache"),
        host_store=hosts,
    )
    exchanges, inference_tokens = [], []

    @app.post("/test/oauth/token")
    async def exchange(request: Request):
        form = parse_qs((await request.body()).decode())
        assert form["federation_policy_id"] == ["test-policy"]
        assert form["grant_type"] == ["urn:ietf:params:oauth:grant-type:token-exchange"]
        exchanges.append(form["subject_token"][0])
        return {
            "access_token": f"inference-{len(exchanges)}",
            "token_type": "Bearer",
            "expires_in": 900,
        }

    @app.post("/test/v1/chat/completions")
    async def infer(request: Request):
        payload = await request.json()
        assert payload["model"] == "wif-test"
        assert payload["stream"] is True
        bearer = request.headers.get("authorization")
        assert bearer == f"Bearer inference-{len(exchanges)}"
        inference_tokens.append(bearer)
        chunks = [
            {"delta": {"role": "assistant", "content": "WIF_OK"}, "finish_reason": None},
            {"delta": {}, "finish_reason": "stop"},
        ]
        body = (
            "".join(
                "data: "
                + json.dumps(
                    {
                        "id": "test-completion",
                        "object": "chat.completion.chunk",
                        "created": 1,
                        "model": "wif-test",
                        "choices": [{"index": 0, **chunk}],
                    }
                )
                + "\n\n"
                for chunk in chunks
            )
            + "data: [DONE]\n\n"
        )
        return StreamingResponse(iter([body]), media_type="text/event-stream")

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
        monkeypatch.setattr(wif, "_TOKEN_URL", base_url + "/test/oauth/token")
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                assert thread.is_alive() and time.monotonic() < deadline
                time.sleep(0.01)
            endpoint = f"{base_url}/v1/hosts/{host_id}/credentials/openrouter"
            assert httpx.get(endpoint, trust_env=False).status_code == 401
            env = {
                "PATH": str(Path(sys.executable).parent) + os.pathsep + os.environ["PATH"],
                "HOME": str(tmp_path),
                "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
                "IS_SANDBOX": "1",
                "OMNIGENT_HOST_TOKEN": "test-launch-token",
                "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            }
            setup = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from omnigent.host.inference_credential import configure_host_inference; "
                    "import sys; configure_host_inference(sys.argv[1], sys.argv[2])",
                    base_url,
                    host_id,
                ],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert setup.returncode == 0, setup.stderr
            config = {
                "providers": {
                    "gateway": {
                        "kind": "gateway",
                        "default": ["pi"],
                        "openai": {
                            "base_url": base_url + "/test/v1",
                            "wire_api": "chat",
                            "auth_command": "python3 -m omnigent.host.inference_credential",
                            "models": {"default": "wif-test"},
                        },
                    }
                }
            }
            provider = resolve_pi_native_provider(model="wif-test", config_loader=lambda: config)
            assert provider is not None
            launch = pi_native_provider_launch(tmp_path / "pi", provider)
            env.update(launch.env)
            for turn in range(2):
                if turn:
                    clock[0] += 901
                    identity.write_text("rotated-identity")
                result = subprocess.run(
                    [
                        pi,
                        *launch.args,
                        "--print",
                        "--no-session",
                        "--no-extensions",
                        "--no-skills",
                        "--no-prompt-templates",
                        "--no-tools",
                        "Say WIF_OK.",
                    ],
                    env=env,
                    cwd=tmp_path,
                    capture_output=True,
                    text=True,
                    timeout=40,
                )
                assert result.returncode == 0, result.stderr + result.stdout
                assert "WIF_OK" in result.stdout
            assert exchanges == ["first-identity", "rotated-identity"]
            assert inference_tokens == ["Bearer inference-1", "Bearer inference-2"]
            hosts.revoke_launch_token(host_id)
            denied = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "omnigent.host.inference_credential",
                ],
                env=env,
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert denied.returncode == 1 and denied.stdout == ""
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()
