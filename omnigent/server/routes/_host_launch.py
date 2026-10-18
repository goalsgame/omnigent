"""Ownership-checked resolution for host runner launches.

Two routes spawn a runner subprocess on a user's host machine and
bind it to a session: ``POST /v1/sessions`` (inline host launch) and
``POST /v1/hosts/{host_id}/runners``. A runner executes arbitrary
tools (shell, file I/O) on the host as that host's user, so a launch
must be authorized against BOTH the host and the session:

- the caller must own the target host (else they could run code on
  another user's machine — cross-user RCE), and
- the caller must own the target session (else they could bind their
  runner to another user's session, or another user's host to their
  session — cross-user hijack / data theft).

Centralizing the checks here keeps the two call sites from drifting
(the original bug was each site enforcing a different subset).
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
from dataclasses import dataclass

from fastapi import HTTPException

from omnigent.entities import Conversation
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.runtime.agent_cache import AgentCache
from omnigent.server.auth import LEVEL_OWNER
from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.permissions import check_session_access
from omnigent.spec import ExecutorSpec
from omnigent.stores import ConversationStore
from omnigent.stores.agent_store import AgentStore
from omnigent.stores.host_store import Host, HostStore, host_is_live
from omnigent.stores.permission_store import PermissionStore


@dataclass
class HostLaunchTarget:
    """A host + session pair the caller is authorized to launch on.

    :param host: The persistent host record (owned by the caller).
    :param conn: The live host WebSocket connection on this replica,
        used to send the launch frame.
    :param conv: The session/conversation the runner will bind to.
    """

    host: Host
    conn: HostConnection
    conv: Conversation


def resolve_host_owner(
    *,
    user_id: str | None,
    host_id: str,
    host_store: HostStore,
) -> Host:
    """
    Authorize that the caller owns a known host.

    Every route that reaches a host on the caller's behalf must pass
    this first so the owner check can't drift between them: the runner
    launch (via :func:`resolve_host_launch`) AND the session-create
    workspace probe, which sends a ``host.stat`` to the host. The
    original bug had that probe contacting another user's host before
    any ownership check. When ``user_id`` is ``None`` (auth disabled)
    the check is skipped, consistent with single-user/local behavior.

    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param host_store: Persistent host registrations.
    :returns: The host record owned by the caller.
    :raises HTTPException: 404 if the host is unknown; 403 if it is
        owned by a different user.
    """
    host = host_store.get_host(host_id)
    if host is None:
        raise HTTPException(status_code=404, detail="host not found")
    if user_id is not None and host.user_id != user_id:
        raise HTTPException(status_code=403, detail="not your host")
    return host


@functools.cache
def _deployment_is_sharded() -> bool:
    """Whether this deployment shards host traffic across replicas by host_id.

    Only the Databricks managed deployment runs multiple replicas behind the
    host_id-sharding router (Dicer); a single-process / OSS server has exactly
    one replica, so an absent host is simply offline. The server can't see the
    sharding layer directly (Dicer strips its routing header), so we detect the
    managed deployment by the presence of the internal lakebox launcher module —
    the same signal that gates ``databricks_features`` in ``server_info`` and
    mirrors the client's own ``isDatabricksWorkspace()`` re-address gate. Cached:
    ``find_spec`` is side-effect-free but the answer is fixed for the process
    lifetime.
    """
    return importlib.util.find_spec("omnigent.onboarding.sandboxes.lakebox") is not None


def host_absent_error(host: Host, *, sharded: bool | None = None) -> OmnigentError:
    """Classify a "host not on this replica" miss for a host-scoped route.

    Every route that reaches a host over its live tunnel looks it up in the
    local (this-replica) ``HostRegistry``. When replicas are sharded by host, a
    request keyed to ``host_id`` can land on a replica that doesn't hold the
    tunnel — the same wrong-replica case ``RunnerRouter`` handles for runner
    dispatch. Tell the two apart from the host record the caller already loaded:

    - **sharded** deployment and still **live** (online + fresh heartbeat) → up
      on some replica, just not here → :data:`~ErrorCode.WRONG_REPLICA` (400) so
      the client re-addresses WITHOUT the key.
    - otherwise → genuinely unreachable here → ``CONFLICT`` (409).

    On a single-replica deployment the local registry is authoritative: an
    absent host is unreachable, full stop. Claiming ``WRONG_REPLICA`` there is a
    lie the client can't satisfy — there is no other replica to re-address to,
    so a stale-but-``online`` DB row (a host that went away without a clean
    ``set_offline`` — version swap, crash) would drive an endless 400 poll loop.
    So ``WRONG_REPLICA`` is emitted only when the deployment is actually sharded.

    :param host: The host's persistent record (owner-checked by the caller).
    :param sharded: Whether this deployment shards by host_id. Defaults to
        auto-detection (:func:`_deployment_is_sharded`); overridable for tests.
    :returns: The ``OmnigentError`` to raise; the global handler maps its code
        to the HTTP status and the ``{"error": {"code": ...}}`` body the
        client's re-address matches on.
    """
    if sharded is None:
        sharded = _deployment_is_sharded()
    if sharded and host_is_live(host):
        return OmnigentError("host is on another replica", code=ErrorCode.WRONG_REPLICA)
    return OmnigentError("host is offline", code=ErrorCode.CONFLICT)


def resolve_host_launch(
    *,
    user_id: str | None,
    host_id: str,
    session_id: str,
    host_store: HostStore,
    host_registry: HostRegistry,
    conversation_store: ConversationStore,
    permission_store: PermissionStore | None,
    conversation: Conversation | None = None,
) -> HostLaunchTarget:
    """
    Resolve and authorize a host runner launch.

    Verifies the host exists, is owned by the caller, and is online,
    and that the caller owns the target session, before any runner is
    spawned. When ``user_id`` is ``None`` (auth disabled) the host-owner
    check is skipped; when ``permission_store`` is ``None`` (auth
    disabled) the session-owner check is skipped — both consistent with
    the single-user/local deployment behavior elsewhere.

    :param user_id: Authenticated caller, e.g. ``"alice@example.com"``,
        or ``None`` when auth is disabled.
    :param host_id: Target host id, e.g. ``"host_a1b2c3d4..."``.
    :param session_id: Session to bind the runner to, e.g.
        ``"conv_abc123"``.
    :param host_store: Persistent host registrations.
    :param host_registry: In-memory live host connections (this replica).
    :param conversation_store: Conversation lookups (also used by the
        session-access check for sub-agent parent delegation).
    :param permission_store: Session permission store, or ``None`` to
        skip the session-owner check (auth disabled).
    :param conversation: Optional authoritative row already loaded for the
        target session. Its id must match ``session_id``.
    :returns: A :class:`HostLaunchTarget` with the validated host,
        connection, and conversation.
    :raises HTTPException: 404 if the host or session is missing (or the
        session is not owned by the caller — 404, not 403, so other
        users' sessions aren't enumerable); 403 if the host is owned by
        a different user.
    :raises OmnigentError: 409 (CONFLICT) if the host is genuinely offline,
        or 400 (WRONG_REPLICA) if it is live but its tunnel is on another
        replica (a wrong-replica landing — the caller re-addresses keyless).
    """
    host = resolve_host_owner(
        user_id=user_id,
        host_id=host_id,
        host_store=host_store,
    )

    conn = host_registry.get(host_id)
    if conn is None:
        raise host_absent_error(host)

    if conversation is not None and conversation.id != session_id:
        raise HTTPException(status_code=404, detail="session not found")
    conv = conversation or conversation_store.get_conversation(session_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="session not found")

    # A runner executes tools as the session's driver, so only the
    # session owner may bind one. A non-owner has no owner-level grant
    # and is rejected. 404 (not 403) avoids leaking the existence of
    # other users' sessions.
    if permission_store is not None:
        allowed = check_session_access(
            user_id,
            session_id,
            LEVEL_OWNER,
            permission_store,
            conversation_store,
            conversation=conv,
        )
        if not allowed:
            raise HTTPException(status_code=404, detail="session not found")

    return HostLaunchTarget(host=host, conn=conn, conv=conv)


async def remember_inference_host_policy(
    conv: Conversation, conn: HostConnection, registry: HostRegistry, store: ConversationStore
) -> None:
    """Remember opt-in placement across host tunnel gaps and server restarts."""
    from omnigent.inference_config import INFERENCE_HOST_POLICY_LABEL

    if not registry.server_inference_harnesses:
        return
    policy = conn.host_id if "server-inference-v1" in conn.hello.capabilities else ""
    if conv.labels.get(INFERENCE_HOST_POLICY_LABEL, "") != policy:
        await asyncio.to_thread(store.set_labels, conv.id, {INFERENCE_HOST_POLICY_LABEL: policy})
        conv.labels[INFERENCE_HOST_POLICY_LABEL] = policy


def inherited_inference_host_policy(
    parent: Conversation, store: ConversationStore, host_id: str
) -> bool:
    from omnigent.inference_config import INFERENCE_HOST_POLICY_LABEL
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    current = parent
    visited: set[str] = set()
    for _ in range(16):
        if current.id in visited:
            break
        visited.add(current.id)
        if current.host_id is not None:
            return (
                current.host_id == host_id
                and current.labels.get(INFERENCE_HOST_POLICY_LABEL) == host_id
            )
        ancestor_id = (
            current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
            if current.kind == "default" and current.labels.get(SIDE_CHAT_LABEL_KEY) == "1"
            else current.parent_conversation_id
        )
        ancestor = store.get_conversation(ancestor_id) if ancestor_id else None
        if ancestor is None:
            break
        current = ancestor
    return False


async def independent_inference_child_host(
    parent: Conversation | None,
    inherited_runner_id: str | None,
    registry: HostRegistry | None,
    store: ConversationStore,
    *,
    host_store: HostStore | None,
    user_id: str | None,
) -> tuple[str, str | None] | None:
    """Keep each child’s process-wide provider configuration independent."""
    from omnigent.runner.routing import routing_host_id

    if parent is None or inherited_runner_id is None or registry is None:
        return None
    if parent.inference_snapshot or not registry.server_inference_harnesses:
        return None
    host_id = await asyncio.to_thread(routing_host_id, parent, store)
    conn = registry.get(host_id) if host_id else None
    if host_id is not None and conn is None:
        if not await asyncio.to_thread(inherited_inference_host_policy, parent, store, host_id):
            return None
        if host_store is not None:
            host = await asyncio.to_thread(
                resolve_host_owner, user_id=user_id, host_id=host_id, host_store=host_store
            )
            raise host_absent_error(host)
        raise HTTPException(
            409, "Parent host is reconnecting; retry child creation once connected"
        )
    if conn is None or "server-inference-v1" not in conn.hello.capabilities:
        return None
    assert host_id is not None
    workspace = await asyncio.to_thread(inherited_runner_workspace, parent, store, host_id)
    return host_id, workspace


def inherited_runner_workspace(
    parent: Conversation, store: ConversationStore, host_id: str
) -> str | None:
    """Find the shared runner's workspace without crossing an explicit host boundary."""
    from omnigent.stores.conversation_store import SIDE_CHAT_LABEL_KEY, SIDE_CHAT_SOURCE_LABEL_KEY

    current = parent
    visited: set[str] = set()
    for _ in range(16):
        if current.id in visited or current.host_id not in (None, host_id):
            return None
        visited.add(current.id)
        if current.workspace:
            return current.workspace
        ancestor_id = (
            current.labels.get(SIDE_CHAT_SOURCE_LABEL_KEY)
            if current.kind == "default" and current.labels.get(SIDE_CHAT_LABEL_KEY) == "1"
            else current.parent_conversation_id
        )
        if not ancestor_id:
            return None
        ancestor = store.get_conversation(ancestor_id)
        if ancestor is None:
            return None
        current = ancestor
    return None


async def agent_has_explicit_auth(
    conv: Conversation,
    agent_store: AgentStore | None = None,
    agent_cache: AgentCache | None = None,
) -> bool:
    """Preserve agent-owned authentication when selecting a host inference route."""
    from omnigent.runtime import get_agent_cache, get_agent_store
    from omnigent.runtime.workflow import _find_spec_by_name

    if conv.agent_id is None:
        return True
    store = agent_store if agent_store is not None else get_agent_store()
    cache = agent_cache if agent_cache is not None else get_agent_cache()
    agent = await asyncio.to_thread(store.get, conv.agent_id)
    if agent is None or agent.bundle_location is None:
        return True
    loaded = await asyncio.to_thread(cache.load, agent.id, agent.bundle_location, expand_env=False)
    spec = loaded.spec
    if conv.sub_agent_name:
        spec = _find_spec_by_name(spec, conv.sub_agent_name) or spec
    return executor_has_explicit_auth(spec.executor)


def executor_has_explicit_auth(executor: ExecutorSpec) -> bool:
    """Recognize both current and legacy agent-owned authentication."""
    return (
        executor.auth is not None
        or bool(getattr(executor, "profile", None))
        or bool(executor.config.get("profile"))
    )
