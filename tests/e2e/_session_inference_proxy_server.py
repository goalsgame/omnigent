"""Real TLS server with a server-only mock provider for inference proxy tests."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import httpx
import uvicorn

from omnigent.db.db_models import workspace_scope
from omnigent.runtime import init as init_runtime
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.app import create_app
from omnigent.server.auth import UnifiedAuthProvider
from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore
from omnigent.stores.host_store import HostStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def main() -> None:
    root = Path(os.environ["OMNIGENT_E2E_ROOT"])
    port = int(os.environ["OMNIGENT_E2E_PORT"])
    db = f"sqlite:///{root / 'server.db'}"
    artifacts = LocalArtifactStore(str(root / "artifacts"))
    agent_store = SqlAlchemyAgentStore(db)
    conversations = SqlAlchemyConversationStore(db)
    files = SqlAlchemyFileStore(db)
    cache = AgentCache(artifact_store=artifacts, cache_dir=root / "cache")
    init_runtime(
        conversation_store=conversations,
        agent_store=agent_store,
        agent_cache=cache,
        file_store=files,
        artifact_store=artifacts,
    )
    named_route = os.environ.get("OMNIGENT_E2E_NAMED_ROUTE") == "1"
    native = os.environ.get("OMNIGENT_E2E_NATIVE", "")
    native_model = {
        "claude-native": "anthropic/claude-test",
        "codex-native": "openai/gpt-test",
    }.get(native)
    native_endpoint = {"claude-native": "messages", "codex-native": "responses"}.get(native)
    budget_test = os.environ.get("OMNIGENT_E2E_BUDGET") == "1"
    if budget_test:
        from omnigent.llms import context_window

        context_window.fetch_model_pricing = lambda model: (
            context_window.ModelPricing(input_per_token=0.001, output_per_token=0.001)
            if model == "test-model"
            else None
        )
    app = create_app(
        agent_store=agent_store,
        file_store=files,
        conversation_store=conversations,
        artifact_store=artifacts,
        agent_cache=cache,
        host_store=HostStore(db),
        permission_store=SqlAlchemyPermissionStore(db),
        auth_provider=UnifiedAuthProvider(source="header", local_single_user=True),
        server_config={
            "openrouter_wif": {"policy_id": "mock-policy"},
            "inference_proxy": {
                "public_url": f"https://127.0.0.1:{port}",
                "signing_key_env": "OMNIGENT_E2E_SIGNING_KEY",
                "models": [native_model or ("friendly-alias" if named_route else "test-model")],
                **({"harnesses": [native]} if native else {}),
                "max_concurrent_requests_per_session": 4,
                "capacity_wait_timeout_seconds": 5,
                **({"session_cost_limits": {"local": 0.001}} if budget_test else {}),
                **(
                    {
                        "upstreams": {
                            "vertex": {
                                "kind": "vertex-openai",
                                "project": "example-project",
                                "location": "global",
                            }
                        },
                        "model_routes": {
                            "friendly-alias": {"upstream": "vertex", "model": "test-model"}
                        },
                    }
                    if named_route and not budget_test
                    else {
                        "upstreams": {"router": {"kind": "openrouter"}},
                        "model_routes": {
                            "friendly-alias": {"upstream": "router", "model": "test-model"}
                        },
                    }
                    if named_route
                    else {}
                ),
            },
        },
    )
    proxy = app.state.inference_proxy
    calls = []

    async def credential():
        return {"token": "server-only-provider"}

    class ProviderTransport(httpx.AsyncBaseTransport):
        def __init__(self):
            self.inner = httpx.AsyncHTTPTransport()

        async def handle_async_request(self, request):
            endpoint = native_endpoint or "chat/completions"
            expected_url = (
                "https://aiplatform.googleapis.com/v1/projects/example-project/locations/global/endpoints/openapi/chat/completions"
                if named_route and not budget_test
                else f"https://openrouter.ai/api/v1/{endpoint}"
            )
            assert request.url.copy_with(query=None) == httpx.URL(expected_url)
            calls.append(
                {
                    "authorized": request.headers["authorization"]
                    == "Bearer server-only-provider",
                    "model": json.loads(request.content)["model"],
                    "vertex": named_route and not budget_test,
                }
            )
            request.url = httpx.URL(os.environ["OMNIGENT_E2E_PROVIDER"] + f"/v1/{endpoint}")
            return await self.inner.handle_async_request(request)

        async def aclose(self):
            await self.inner.aclose()

    async def google_credential():
        return "server-only-provider"

    proxy.google_credential.credential = google_credential
    proxy.broker.credential = credential
    proxy.transport = ProviderTransport()

    @app.get("/v1/_e2e/proxy_requests")
    async def requests():
        return {"calls": calls, "active": proxy.active_requests, "queued": proxy.queued_requests}

    app.router.routes.insert(0, app.router.routes.pop())

    @app.post("/v1/_e2e/sessions/{sid}/initialize")
    async def initialize(sid: str):
        conv = await asyncio.to_thread(conversations.get_conversation, sid)
        routed = app.state.runner_router.client_for_existing_conversation(sid)
        response = await app.state.runner_session_initializer.initialize(
            conv,
            routed.client,
            timeout=20,
            suppress_recovery_turn=True,
        )
        if budget_test:
            from omnigent.server.routes import sessions

            await sessions._ensure_runner_relay_ready(
                sid, conv.runner_id, routed.client, conversations, conversation=conv
            )
        connection = app.state.tunnel_registry.get(conv.runner_id)
        return {
            "status": response.status_code,
            "payload": response.json(),
            "generation": connection.generation,
        }

    @app.post("/v1/_e2e/sessions/{sid}/inference-grant")
    async def inference_grant(sid: str):
        conv = await asyncio.to_thread(conversations.get_conversation, sid)
        owner = await asyncio.to_thread(
            conversations.get_session_owner_authority, sid, owner_only=True
        )
        return proxy.launch_config(
            conv, conv.runner_id, "openai-agents", conv.host_id, owner.user_id, owner.generation
        )["server_inference"]

    @app.post("/v1/_e2e/sessions/{sid}/recycle")
    async def recycle(sid: str):
        conv = await asyncio.to_thread(conversations.get_conversation, sid)
        registry = app.state.tunnel_registry
        connection = registry.get(conv.runner_id)
        generation = connection.generation
        registry.deregister(conv.runner_id, session=connection)
        async with asyncio.timeout(30):
            while True:
                replacement = registry.get(conv.runner_id)
                if replacement is not None and replacement.generation != generation:
                    return {"generation": replacement.generation}
                await asyncio.sleep(0.05)

    # Place test routes ahead of the SPA fallback.
    for _ in range(3):
        app.router.routes.insert(0, app.router.routes.pop())

    workspace = int(os.environ.get("OMNIGENT_E2E_WORKSPACE", "0"))

    async def scoped_app(scope, receive, send):
        # Refresh/inference arrive at the global origin without tenant context.
        selected = 0 if scope.get("path", "").startswith("/v1/inference/") else workspace
        with workspace_scope(selected):
            await app(scope, receive, send)

    uvicorn.run(
        scoped_app,
        host="127.0.0.1",
        port=port,
        ssl_keyfile=str(root / "key.pem"),
        ssl_certfile=str(root / "cert.pem"),
    )


if __name__ == "__main__":
    main()
