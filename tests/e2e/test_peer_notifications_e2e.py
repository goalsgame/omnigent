"""Peer HTTP send -> background delivery -> runner -> actual Pi, with a local model."""

from __future__ import annotations

import asyncio
import contextlib
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from omnigent.harnesses.pi_native import bridge
from omnigent.inner.terminal import TerminalInstance
from omnigent.runner import create_runner_app
from omnigent.server.peer_notifications import PeerNotifier
from omnigent.server.routes.peer_messages import create_peer_messages_router
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.peer_delivery_store import PeerDeliveryStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from omnigent.terminals import TerminalRegistry
from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


@pytest.mark.asyncio
@pytest.mark.skipif(shutil.which("pi") is None, reason="native Pi CLI is required")
async def test_peer_send_automatically_starts_idle_pi_turn(tmp_path, db_uri, monkeypatch):
    """No user prompt, fake notification receipt, or stubbed delivery HTTP responses."""
    calls = []

    class Model(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            calls.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for delta, finish in [
                ({"role": "assistant", "content": "Inbox notification received."}, None),
                ({}, "stop"),
            ]:
                chunk = {
                    "id": "test",
                    "object": "chat.completion.chunk",
                    "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
                }
                self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()

    model = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Model)
    thread = threading.Thread(target=model.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    agent_id = uuid.uuid4().hex
    conversations = SqlAlchemyConversationStore(db_uri)
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user("local")
    source, target = [conversations.create_conversation(agent_id=agent_id).id for _ in range(2)]
    for sid in (source, target):
        permissions.grant("local", sid, 4)
    directory = bridge.prepare_bridge_dir(target)
    extension, config = bridge.write_extension_files(
        directory, session_id=target, server_url="", conversation_url=""
    )
    pi_agent = tmp_path / "pi-agent"
    pi_agent.mkdir()
    (pi_agent / "models.json").write_text(
        json.dumps(
            {
                "providers": {
                    "notification-test": {
                        "api": "openai-completions",
                        "baseUrl": f"http://127.0.0.1:{model.server_port}/v1",
                        "apiKey": "test-key",
                        "models": [
                            {
                                "id": "notification-model",
                                "name": "Notification test",
                                "reasoning": False,
                                "input": ["text"],
                                "contextWindow": 32000,
                                "maxTokens": 1000,
                                "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                            }
                        ],
                    }
                }
            }
        )
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not any(
            part in key
            for part in (
                "API_KEY",
                "AUTH_TOKEN",
                "OPENAI",
                "ANTHROPIC",
                "GOOGLE",
                "GEMINI",
                "OPENROUTER",
            )
        )
    }
    env.update(PI_CODING_AGENT_DIR=str(pi_agent), OMNIGENT_PI_NATIVE_CONFIG=str(config))
    spec = AgentSpec(
        spec_version=1,
        name="notification-test",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}),
    )

    async def resolve(agent_id, session_id=None):
        return spec

    process = None

    class ConnectedPiTerminal(TerminalInstance):
        async def is_alive(self):
            return process is not None and process.poll() is None

    registry = TerminalRegistry()
    instance = ConnectedPiTerminal(
        name="pi",
        session_key="main",
        socket_path=tmp_path / "unused-socket",
        private_dir=tmp_path,
        env={bridge.PI_NATIVE_CONFIG_ENV_VAR: str(config)},
    )
    registry._by_conversation.setdefault(target, {})[("pi", "main")] = instance
    runner = create_runner_app(
        terminal_registry=registry,
        process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),
        spec_resolver=resolve,
        server_client=NullServerClient(),
    )
    task = None
    process = None
    with (tmp_path / "pi.log").open("w") as log:
        try:
            process = subprocess.Popen(
                [
                    shutil.which("pi"),
                    "--mode",
                    "rpc",
                    "--no-session",
                    "--no-extensions",
                    "--no-skills",
                    "--no-prompt-templates",
                    "--no-tools",
                    "--extension",
                    str(extension),
                    "--provider",
                    "notification-test",
                    "--model",
                    "notification-model",
                ],
                stdin=subprocess.PIPE,
                stdout=log,
                stderr=subprocess.STDOUT,
                cwd=tmp_path,
                env=env,
            )
            deadline = time.monotonic() + 30
            while not (directory / "input_ready").exists() and time.monotonic() < deadline:
                assert process.poll() is None, (tmp_path / "pi.log").read_text()
                await asyncio.sleep(0.05)
            assert (directory / "input_ready").exists()
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=runner), base_url="http://runner"
            ) as runner_client:
                store = PeerDeliveryStore(db_uri)
                agent = SimpleNamespace(
                    id=agent_id, bundle_location="fixture", operator_authored=False
                )
                state = SimpleNamespace(
                    runner_router=SimpleNamespace(
                        client_for_session_resources=lambda *args, **kwargs: SimpleNamespace(
                            client=runner_client
                        )
                    )
                )
                notifier = PeerNotifier(
                    store,
                    state,
                    conversations,
                    SimpleNamespace(get=lambda _: agent),
                    SimpleNamespace(load=lambda *a, **kw: SimpleNamespace(spec=spec)),
                )
                server = FastAPI()
                server.include_router(
                    create_peer_messages_router(
                        store, conversations, permissions, None, notifier=notifier
                    ),
                    prefix="/v1",
                )
                task = asyncio.create_task(notifier.run())
                assert calls == []
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=server), base_url="http://server"
                ) as client:
                    result = await client.post(
                        f"/v1/sessions/{source}/peer-messages",
                        json={
                            "id": uuid.uuid4().hex,
                            "recipient_session_id": target,
                            "subject": "Ready",
                            "body": "Please read your inbox.",
                        },
                    )
                    assert result.status_code == 201, result.text
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline and (not calls or store.candidates()):
                    await asyncio.sleep(0.05)
                assert calls, (tmp_path / "pi.log").read_text()
                assert "Peer messages are waiting" in json.dumps(calls[0]["messages"])
                receipts = list((directory / "peer-receipts").iterdir())
                assert len(receipts) == 1
                assert store.candidates() == []
                assert store.list_messages(target, unread_only=True)[0].acknowledged_at is None
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
                task = None
        finally:
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            if process is not None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    await asyncio.to_thread(process.wait)
            model.shutdown()
            model.server_close()
            thread.join(timeout=5)
