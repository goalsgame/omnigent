"""Managed-sandbox model previews and saved session-policy validation."""

from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inference_config import (
    binding_for_harness,
    normalize_inference_harness,
    parse_inference_config,
    resolve_bound_provider,
)
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._session_create_validation import validate_session_agent


def inference_service(request: Request) -> Any:
    """Use the service owned by this server's application state."""
    from omnigent.server.inference_catalog import SandboxInferenceService

    service = getattr(request.app.state, "inference_catalog", None)
    if service is None:
        service = SandboxInferenceService(request.app.state)
        request.app.state.inference_catalog = service
    return service


def actual_harness(spec: Any, override: str | None = None) -> str:
    """Preserve ACP slugs and native identities when selecting a profile binding."""
    declared = spec.executor.config.get("harness") or spec.executor.type
    if override == "acp" and declared.startswith("acp:"):
        return declared
    return normalize_inference_harness(override or declared)


def configured_snapshot(snapshot: dict[str, Any] | None) -> bool:
    """Whether the session's own harness has an inference binding."""
    return (
        snapshot is not None
        and binding_for_harness(snapshot["runtime_config"], snapshot["harness"]) is not None
    )


# This reserved label is server-authored and carries no provider credentials.
PROXY_POLICY_LABEL = "omnigent.sandbox.inference_policy"


def proxy_policy(conversation: Conversation) -> dict[str, Any] | None:
    value = (conversation.labels or {}).get(PROXY_POLICY_LABEL)
    return json.loads(value) if value else None


def _declares_direct_sdk_auth(config: dict[str, Any], harness: str) -> bool:
    """Recognize runner credential sources without resolving pod-only secrets."""
    from omnigent.onboarding.provider_config import load_providers

    if harness != "openai-agents":
        return False
    auth = config.get("auth")
    if isinstance(auth, dict) and auth.get("type") in {"api_key", "databricks"}:
        return True
    for entry in load_providers(config).values():
        if entry.kind in {"subscription", "cli-config", "bedrock"}:
            continue
        if entry.kind == "databricks" and entry.profile:
            return True
        family = entry.families.get("openai")
        if family is not None and (
            entry.kind == "local" or family.api_key or family.api_key_ref or family.auth_command
        ):
            return True
    return False


def managed_proxy_catalog(
    request: Request,
    provider: str | None,
    harness: str,
    *,
    agent_auth: object = None,
    profile: str | None = None,
) -> dict[str, Any] | None:
    """Preview proxy models without creating a direct-provider session snapshot."""
    from omnigent.onboarding.provider_config import default_provider_for_harness

    proxy = getattr(request.app.state, "inference_proxy", None)
    deployment = getattr(request.app.state, "sandbox_config", None)
    target = deployment.for_provider(provider) if deployment is not None else None
    harness = normalize_inference_harness(harness)
    if (
        proxy is None
        or not proxy.config.managed_sandboxes
        or (harness != "auto" and harness not in proxy.config.harnesses)
        or target is None
        or target.provider not in ("agent_sandbox", "kubernetes")
        or agent_auth is not None
        or profile
        or parse_inference_config(target.host_config or {})
        or default_provider_for_harness(target.host_config or {}, harness) is not None
        or _declares_direct_sdk_auth(target.host_config or {}, harness)
    ):
        return None
    models = list(proxy.config.model_apis(harness)) if harness in proxy.config.harnesses else []
    return {
        "configured": True,
        "configuration_revision": None,
        "provider_label": "Server inference",
        "default_model": models[0] if models else None,
        "status": "ready" if models else "empty",
        "models": [
            {"id": model, "model": model, "displayName": model, "isDefault": i == 0}
            for i, model in enumerate(models)
        ],
    }


def proxy_policy_catalog(request: Request | None, policy: dict[str, Any]) -> dict[str, Any]:
    """Resolve a saved proxy policy, including when its runner is offline."""
    if request is not None:
        try:
            catalog = managed_proxy_catalog(request, policy["provider"], policy["harness"])
            if catalog is not None:
                return catalog
        except (ValueError, OmnigentError):
            pass
    return {
        "configured": True,
        "models": [],
        "status": "unavailable",
        "default_model": None,
        "error": "This session's server inference policy is unavailable",
    }


def managed_inference_configured(request: Request, provider: str | None) -> bool:
    """Whether a managed target requires an identifiable harness before creation."""
    deployment = getattr(request.app.state, "sandbox_config", None)
    target = deployment.for_provider(provider) if deployment is not None else None
    return bool(
        target
        and (
            parse_inference_config(target.host_config or {})
            or managed_proxy_catalog(request, provider, "auto")
        )
    )


def selected_catalog_model(catalog: dict[str, Any], model: str | None) -> str:
    """Reject unavailable selections before persistence or prompt dispatch."""
    if catalog.get("status") != "ready":
        raise OmnigentError(
            catalog.get("error") or "No available models match this harness's configuration",
            code=ErrorCode.INVALID_INPUT,
        )
    selected = model or catalog.get("default_model")
    if not selected or selected not in {row["id"] for row in catalog["models"]}:
        raise OmnigentError(
            f"Model {selected!r} is not available in this session's configured model list",
            code=ErrorCode.INVALID_INPUT,
        )
    return selected


async def prepare_create_inference(
    request: Request,
    body: Any,
    spec: Any,
    user_id: str | None,
    conversation_store: Any,
    *,
    harness_override: str | None = None,
    model_override: str | None = None,
) -> tuple[dict[str, Any] | None, str | None, dict[str, str]]:
    """Capture the accepted configuration before a session or sandbox exists."""
    harness = actual_harness(spec, harness_override)
    if (
        harness == "auto"
        and body.host_type == "managed"
        and managed_inference_configured(request, body.sandbox_provider)
    ):
        raise OmnigentError(
            "Choose a harness to use this sandbox's configured inference providers",
            code=ErrorCode.INVALID_INPUT,
        )
    snapshot = None
    inherited_proxy_policy = None
    if body.parent_session_id:
        parent = await asyncio.to_thread(
            conversation_store.get_conversation, body.parent_session_id
        )
        if parent is not None and parent.inference_snapshot is not None:
            snapshot = copy.deepcopy(parent.inference_snapshot)
            snapshot["harness"] = harness
        if (
            parent is not None
            and body.host_type != "managed"
            and (body.host_id is None or body.host_id == parent.host_id)
            and spec.executor.auth is None
            and not (spec.executor.profile or spec.executor.config.get("profile"))
        ):
            inherited_proxy_policy = proxy_policy(parent)
    if snapshot is None and (body.host_type == "managed" or inherited_proxy_policy is not None):
        provider = (
            inherited_proxy_policy["provider"] if inherited_proxy_policy else body.sandbox_provider
        )

        proxy_catalog = managed_proxy_catalog(
            request,
            provider,
            harness,
            agent_auth=spec.executor.auth,
            profile=spec.executor.profile or spec.executor.config.get("profile"),
        )
        if proxy_catalog is not None:
            if body.inference_configuration_revision is not None:
                raise HTTPException(
                    409, "Harness configuration changed. Refresh the model choices."
                )
            if (
                getattr(body, "cost_control_mode_override", None) == "on"
                or getattr(body, "subagent_routing_override", None) == "on"
                or harness_override == "auto"
            ):
                raise OmnigentError(
                    "Automatic provider routing is unavailable for server inference",
                    code=ErrorCode.INVALID_INPUT,
                )
            selected = (
                model_override if model_override is not None else (spec.executor.model or None)
            )
            target = request.app.state.sandbox_config.for_provider(provider)
            assert target is not None
            return (
                None,
                selected_catalog_model(proxy_catalog, selected),
                {
                    PROXY_POLICY_LABEL: json.dumps(
                        {"provider": target.provider, "harness": harness}
                    )
                },
            )
        if inherited_proxy_policy is not None:
            raise OmnigentError(
                "The child harness is unavailable in its parent's server inference policy",
                code=ErrorCode.INVALID_INPUT,
            )
        snapshot = await inference_service(request).prepare(
            body.sandbox_provider, harness, user_id, agent_auth=spec.executor.auth
        )
        revision = body.inference_configuration_revision
        if revision is not None and (
            snapshot is None or revision != snapshot["configuration_revision"]
        ):
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "inference_configuration_changed",
                    "message": "Harness configuration changed. Refresh the model choices.",
                },
            )
    if snapshot is not None and harness == "auto":
        raise OmnigentError(
            "Choose a harness to use the parent session's saved inference providers",
            code=ErrorCode.INVALID_INPUT,
        )
    if not configured_snapshot(snapshot):
        return snapshot, model_override, {}
    assert snapshot is not None
    resolve_bound_provider(snapshot["runtime_config"], harness, spec.executor.auth)
    if spec.executor.profile:
        raise OmnigentError(
            "The agent's Databricks profile conflicts with the configured harness provider",
            code=ErrorCode.INVALID_INPUT,
        )
    if (
        getattr(body, "cost_control_mode_override", None) == "on"
        or getattr(body, "subagent_routing_override", None) == "on"
        or harness_override == "auto"
    ):
        raise OmnigentError(
            "Automatic provider routing is unavailable for a bound inference profile",
            code=ErrorCode.INVALID_INPUT,
        )
    catalog = await inference_service(request).catalog(snapshot)
    snapshot["catalog"] = catalog
    selected = selected_catalog_model(catalog, model_override)
    return snapshot, selected, {}


async def validate_saved_selection(
    request: Request, conversation: Conversation, model: str | None
) -> str | None:
    """Recheck availability using saved provider/credential identity, never current defaults."""
    policy = proxy_policy(conversation)
    if policy is not None:
        return selected_catalog_model(proxy_policy_catalog(request, policy), model)
    snapshot = conversation.inference_snapshot
    if not configured_snapshot(snapshot):
        return model
    catalog = await inference_service(request).catalog(snapshot)
    return selected_catalog_model(catalog, model)


def create_sandbox_inference_router(
    *,
    agent_store: Any,
    agent_cache: Any,
    conversation_store: Any,
    permission_store: Any,
    auth_provider: AuthProvider | None,
) -> APIRouter:
    """Expose authenticated previews without provisioning a host."""
    router = APIRouter()

    @router.get("/sandbox-providers/{provider}/harnesses/{harness}/model-options")
    async def model_options(
        request: Request, provider: str, harness: str, agent_id: str | None = None
    ) -> dict[str, Any]:
        user_id = require_user(request, auth_provider)
        auth = None
        profile = None
        if agent_id is not None:
            # Clients read a missing agent's 404 from ``detail``; keep that shape.
            if await asyncio.to_thread(agent_store.get, agent_id) is None:
                raise HTTPException(status_code=404, detail="Agent not found")
            # The same check as binding the agent: its owner, or READ on any
            # session that shares the agent row.
            agent = await validate_session_agent(
                user_id=user_id,
                agent_id=agent_id,
                agent_store=agent_store,
                permission_store=permission_store,
                conversation_store=conversation_store,
            )
            spec = (
                await asyncio.to_thread(
                    agent_cache.load,
                    agent.id,
                    agent.bundle_location,
                    expand_env=agent.operator_authored,
                )
            ).spec
            harness = actual_harness(spec, harness)
            auth = spec.executor.auth
            profile = spec.executor.profile or spec.executor.config.get("profile")
        try:
            proxy_catalog = managed_proxy_catalog(
                request, provider, harness, agent_auth=auth, profile=profile
            )
            if proxy_catalog is not None:
                return proxy_catalog
            snapshot = await inference_service(request).prepare(
                provider, harness, user_id, agent_auth=auth
            )
            if snapshot is not None:
                return snapshot["catalog"]
        except (OmnigentError, ValueError) as exc:
            return {
                "configured": True,
                "models": [],
                "configuration_revision": None,
                "provider_label": None,
                "default_model": None,
                "status": "unavailable",
                "error": exc.message
                if isinstance(exc, OmnigentError)
                else "Invalid inference configuration",
            }
        return {
            "configured": False,
            "models": [],
            "configuration_revision": None,
            "provider_label": None,
            "default_model": None,
            "status": "unconfigured",
        }

    return router
