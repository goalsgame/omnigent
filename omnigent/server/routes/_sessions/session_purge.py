"""Server-side session purge shared by the delete route and retention sweeps."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
from fastapi import Request

from omnigent.entities import Conversation
from omnigent.errors import OmnigentError
from omnigent.server.routes._sessions.common import (
    _intentional_stop_sessions,
    _interrupt_fenced_sessions,
    _logger,
    _pushed_model_options_cache,
    _session_mcp_startup_cache,
    _session_sandbox_status_cache,
)
from omnigent.server.routes._sessions.helpers import (
    _get_runner_client_for_resource_access,
    _prune_session_read_state,
    _remove_session_worktree_best_effort,
)
from omnigent.server.routes._sessions.orchestration import _best_effort_stop


async def purge_session(
    session_id: str,
    conv: Conversation,
    *,
    conversation_store: Any,
    runner_router: Any,
    file_store: Any | None,
    artifact_store: Any | None,
    app_state: Any,
    request: Request | None = None,
    delete_branch: bool = False,
) -> bool:
    """Stop a session, release its runner, sandbox and files, and delete its rows.

    Runner-side cleanup is best-effort. Must run inside the session's workspace
    scope.

    :param session_id: Session/conversation identifier, e.g. ``"conv_abc123"``.
    :param conv: The session's conversation row.
    :param conversation_store: Store holding the conversation.
    :param runner_router: Runner router, or ``None`` in in-process setups.
    :param file_store: Session file store, or ``None``.
    :param artifact_store: Blob store backing session files, or ``None``.
    :param app_state: ``app.state`` supplying managed launches, host store and
        sandbox config.
    :param request: Request for host access; required for ``delete_branch``.
    :param delete_branch: Remove the session's git worktree and branch.
    :returns: ``False`` if the conversation row was already gone.
    """
    await _best_effort_stop(session_id, conversation_store, runner_router)
    # Runner-side resource cleanup is best-effort: if the bound
    # runner is offline or unbound, the session must still be
    # deletable. Server-owned records (files and conversation row
    # below) live independently of the runner, and runner-side
    # resources are gone with the runner anyway.
    runner_client: httpx.AsyncClient | None = None
    try:
        runner_client = await _get_runner_client_for_resource_access(session_id)
    except OmnigentError as exc:
        _logger.info(
            "Skipping runner-side cleanup for %s; proceeding with server-side delete: %s",
            session_id,
            exc,
        )
    if runner_client is not None:
        try:
            # Allow initialization, forwarder, and resource cleanup to finish.
            await runner_client.delete(
                f"/v1/sessions/{session_id}",
                timeout=60.0,
            )
        except (httpx.HTTPError, ConnectionError):
            _logger.warning(
                "Runner cleanup failed for %s, falling back",
                session_id,
            )
    else:
        import contextlib

        from omnigent.runtime import get_terminal_registry

        with contextlib.suppress(RuntimeError):
            await get_terminal_registry().cleanup_conversation(session_id)
    # Opt-in git worktree cleanup: only when delete_branch=true and
    # the session has a server-created worktree. Runs after runner
    # teardown but before the irreversible file cleanup below: an
    # unreachable host fails the delete (409) with the session
    # retained, so nothing irrecoverable may be destroyed first.
    # Git errors on a reachable host stay best-effort.
    if (
        delete_branch
        and request is not None
        and conv.git_branch is not None
        and conv.workspace is not None
        and conv.host_id is not None
    ):
        from omnigent.server.routes._host_worktree import WORKTREE_ROOT_LABEL_KEY

        await _remove_session_worktree_best_effort(
            host_id=conv.host_id,
            worktree_path=conv.workspace,
            branch=conv.git_branch,
            delete_branch=True,
            request=request,
            reason="session-delete",
            expected_root_fingerprint=conv.labels.get(WORKTREE_ROOT_LABEL_KEY),
            conversation_store=conversation_store,
            exclude_conversation_id=conv.id,
            fail_if_unavailable=True,
        )
    # Session file cleanup. delete_all_for_session returns only the blob
    # keys that became orphaned — a blob still shared by a fork in another
    # session is not returned, so the fork's attachment survives.
    if file_store is not None and artifact_store is not None:
        orphaned_blob_keys = await asyncio.to_thread(file_store.delete_all_for_session, session_id)
        for blob_key in orphaned_blob_keys:
            await asyncio.to_thread(artifact_store.delete, blob_key)
    _interrupt_fenced_sessions.discard(session_id)
    _intentional_stop_sessions.discard(session_id)
    deleted = await conversation_store.delete_conversation(session_id)
    if not deleted:
        return False
    # The session is gone, so is its launch-progress state. Failed
    # launches are retained in the cache for reload visibility while
    # the session exists; without this eviction every deleted
    # failed-launch session would leak one entry for the process
    # lifetime.
    _session_sandbox_status_cache.pop(session_id, None)
    # Same for MCP startup state: failed/cancelled maps are retained
    # for reload visibility while the session exists, so a session
    # whose MCP startup never settled clean would leak its entry.
    _session_mcp_startup_cache.pop(session_id, None)
    # Same for the extension-pushed model catalog: kept across reloads
    # while the session exists (the extension only pushes on start), so a
    # deleted session would otherwise leak its entry for the process life.
    _pushed_model_options_cache.pop(session_id, None)
    # Drop the deleted session's per-user read-state from every user's
    # caches so they don't accumulate orphan entries for the process
    # lifetime.
    _prune_session_read_state(session_id)
    # Same for the tracker's entry — a deleted session's launch can
    # never be rendezvoused again (access checks 404 first), so a
    # retained failure is dead weight. ``finish`` also settles a
    # still-in-flight entry, releasing any parked message POST into
    # its session re-read (which now correctly 404s); the background
    # task's later ``fail`` on the popped entry is a no-op.
    managed_launches_for_delete = getattr(app_state, "managed_launches", None)
    if managed_launches_for_delete is not None:
        managed_launches_for_delete.finish(session_id)
    # Managed-host cleanup: when the session's host is backed by a
    # server-provisioned sandbox (host_type="managed"), terminate
    # the sandbox and delete the host row — which also revokes its
    # launch token. Best-effort by design — the provider's lifetime
    # cap reaps stragglers. External (laptop) hosts have no
    # sandbox_id and are never touched.
    host_store_for_managed = getattr(app_state, "host_store", None)
    if conv.host_id is not None and host_store_for_managed is not None:
        bound_host = await asyncio.to_thread(host_store_for_managed.get_host, conv.host_id)
        if bound_host is not None and bound_host.sandbox_provider is not None:
            from omnigent.server.managed_hosts import terminate_managed_host

            await terminate_managed_host(
                bound_host,
                host_store_for_managed,
                # Supplies the launcher for the provider-side
                # terminate; None (config removed since launch)
                # still deletes the row and revokes the token.
                getattr(app_state, "sandbox_config", None),
            )
    return True
