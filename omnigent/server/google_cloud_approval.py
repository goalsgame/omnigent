"""Personal Google Cloud consent through the session elicitation protocol."""

from __future__ import annotations

import asyncio
import json
import secrets
import shlex
from dataclasses import dataclass
from typing import Any

from fastapi import Request
from pydantic import ValidationError

from omnigent.db.workspace_cache import WorkspaceScopedCache
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.policies.builtins._shell import (
    real_invocation_tokens,
    split_command_segments,
    unwrap_shell_command,
)
from omnigent.runtime import session_stream
from omnigent.server.auth import RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.schemas import ElicitationResult

_TIMEOUT = 300.0
_PREFIX = "elicit_google_cloud_"


@dataclass
class _Approval:
    id: str
    context: dict[str, Any]
    store: Any
    future: asyncio.Future[bool]
    timer: asyncio.TimerHandle
    resolving: bool = False


_pending: WorkspaceScopedCache[str, _Approval] = WorkspaceScopedCache()


def _finish(pending: _Approval, allowed: bool, action: str) -> None:
    if _pending.get(pending.id) is not pending:
        return
    _pending.pop(pending.id, None)
    pending.timer.cancel()
    if not pending.future.done():
        pending.future.set_result(allowed)
    session_stream.publish(
        pending.context["session_id"],
        {
            "type": "response.elicitation_resolved",
            "elicitation_id": pending.id,
            "action": action,
        },
    )


async def request_google_cloud_access(store: Any, host_id: str) -> _Approval | None:
    """Publish one deduplicated native prompt per current sandbox/account grant."""
    context = await asyncio.to_thread(store.access.host_request, host_id)
    if context is None or context["state"] != "pending":
        return None
    for pending in list(_pending.values()):
        if pending.context == context:
            return pending
        if pending.context["host_id"] == host_id:
            _finish(pending, False, "cancel")
    loop = asyncio.get_running_loop()
    elicitation_id = _PREFIX + secrets.token_hex(16)
    future: asyncio.Future[bool] = loop.create_future()
    pending = _Approval(
        elicitation_id,
        context,
        store,
        future,
        loop.call_later(_TIMEOUT, lambda: _finish(pending, False, "cancel")),
    )
    _pending[elicitation_id] = pending
    session_stream.publish(
        context["session_id"],
        {
            "type": "response.elicitation_request",
            "elicitation_id": elicitation_id,
            "method": "elicitation/create",
            "params": {
                "mode": "form",
                "message": (
                    f"Allow this sandbox to use Google Cloud as {context['email']}? "
                    "Only the session owner can approve. This grants all processes and "
                    "subagents in the sandbox the account's Google Cloud permissions "
                    "until access is revoked or the account or sandbox changes. "
                    "Previously issued tokens may remain valid for up to one hour "
                    "after revocation."
                ),
                "requestedSchema": {"type": "object", "properties": {}},
                "phase": "tool_call",
                "policy_name": "Google Cloud access",
            },
        },
    )
    return pending


async def resolve_google_cloud_access(
    session_id: str,
    data: dict[str, Any],
    request: Request | None,
    auth_provider: AuthProvider | None,
) -> bool:
    """Handle protected consent before generic collaborator/runner resolution."""
    eid = data.get("elicitation_id")
    if not isinstance(eid, str) or not eid.startswith(_PREFIX):
        return False
    pending = _pending.get(eid)
    if pending is None:
        raise OmnigentError(
            "Google Cloud approval expired; retry the command", code=ErrorCode.INVALID_INPUT
        )
    user = auth_provider.get_credential_user_id(request) if request and auth_provider else None
    if (
        not user
        or user == RESERVED_USER_LOCAL
        or user.startswith("oidc-machine:")
        or user != pending.context["user_id"]
        or session_id != pending.context["session_id"]
    ):
        raise OmnigentError(
            "Only the directly authenticated session owner can approve Google Cloud access",
            code=ErrorCode.FORBIDDEN,
        )
    try:
        verdict = ElicitationResult.model_validate(
            {k: v for k, v in data.items() if k != "elicitation_id"}
        )
    except ValidationError:
        raise OmnigentError("Invalid approval verdict", code=ErrorCode.INVALID_INPUT) from None
    if pending.resolving:
        raise OmnigentError("Approval is already being resolved", code=ErrorCode.INVALID_INPUT)
    pending.resolving = True
    pending.timer.cancel()
    allowed = verdict.action == "accept"
    try:
        await asyncio.to_thread(
            pending.store.access.session,
            session_id,
            user,
            decision="allowed" if allowed else "denied",
            generation=pending.context["generation"],
            expected_host=pending.context["host_id"],
        )
    except BaseException as exc:
        # Settings may have committed before its prompt notification runs.
        committed_allowed = False
        try:
            current = await asyncio.to_thread(
                pending.store.access.session,
                session_id,
                user,
                generation=pending.context["generation"],
                expected_host=pending.context["host_id"],
            )
            committed_allowed = current["state"] == "allowed"
        except (PermissionError, ValueError):
            pass
        finally:
            _finish(pending, committed_allowed, "accept" if committed_allowed else "cancel")
        if not isinstance(exc, (PermissionError, ValueError)):
            raise
        if committed_allowed:
            return True
        raise OmnigentError(
            "Google Cloud approval is no longer current; retry the command",
            code=ErrorCode.INVALID_INPUT,
        ) from None
    _finish(pending, allowed, verdict.action)
    return True


def cloud_command(data: Any) -> bool:
    """Preflight common cloud CLIs; broker consent remains the security boundary."""
    if not isinstance(data, dict):
        return False
    args = data.get("arguments", data)
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return False
    if not isinstance(args, dict):
        return False
    command = args.get("command", args.get("cmd"))
    if isinstance(command, list):
        command = " ".join(str(part) for part in command)
    if not isinstance(command, str):
        return False
    return _cloud_shell_command(command)


def _cloud_shell_command(command: str, depth: int = 0) -> bool:
    if depth > 8:
        return False
    for segment in split_command_segments(command):
        try:
            tokens = real_invocation_tokens(shlex.split(segment))
        except ValueError:
            continue
        if not tokens:
            continue
        nested = unwrap_shell_command(tokens)
        if nested and _cloud_shell_command(nested, depth + 1):
            return True
        program = tokens[0].rsplit("/", 1)[-1]
        if (
            program == "gcloud"
            and len(tokens) > 1
            and tokens[1] not in ("version", "--version", "help", "--help", "-h")
        ):
            return True
        if program == "terraform":
            arguments = [token for token in tokens[1:] if not token.startswith("-")]
            if arguments and arguments[0] in (
                "plan",
                "apply",
                "destroy",
                "import",
                "refresh",
                "console",
                "test",
                "init",
            ):
                return True
    return False


async def preflight_google_cloud(request: Request, host_id: str | None, data: Any) -> str | None:
    """Park cloud tool calls before execution; return a denial reason on refusal."""
    store = getattr(request.app.state, "google_cloud_store", None)
    if store is None or not host_id or not cloud_command(data):
        return None
    pending = await request_google_cloud_access(store, host_id)
    if pending is not None:
        # A disconnected waiter must not cancel consent shared by other commands.
        approved = await asyncio.shield(pending.future)
        if not approved:
            return "Google Cloud access was not approved; command not started."
    context = await asyncio.to_thread(store.access.host_request, host_id)
    # Unconnected accounts can still use explicitly supplied CLI credentials.
    if pending is None and context is not None and context["state"] == "not_connected":
        return None
    if context is None or (
        pending is not None
        and any(
            context[k] != pending.context[k]
            for k in ("host_id", "generation", "user_id", "session_id")
        )
    ):
        return "Google Cloud account or sandbox changed; retry the command."
    if context["state"] != "allowed":
        return "Google Cloud access was not approved by the session owner; command not started."
    return None


def settle_google_cloud_access(
    session_id: str, user_id: str, generation: str, decision: str
) -> None:
    """Close native prompts when the owner decides through connection settings."""
    for pending in list(_pending.values()):
        context = pending.context
        if (context["session_id"], context["user_id"], context["generation"]) == (
            session_id,
            user_id,
            generation,
        ):
            _finish(
                pending, decision == "allowed", "accept" if decision == "allowed" else "decline"
            )


def cancel_google_cloud_access(session_id: str) -> None:
    """Cancel pending credential grants when their root session is stopped."""
    for pending in list(_pending.values()):
        if pending.context["session_id"] == session_id:
            _finish(pending, False, "cancel")
