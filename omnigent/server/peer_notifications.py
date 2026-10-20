"""Deliver durable inbox notifications over the existing runner event channel."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from omnigent.db.account_authority import account_authority_scope
from omnigent.db.db_models import workspace_scope
from omnigent.stores.peer_delivery_store import DeliveryPolicy, PeerDeliveryStore

_logger = logging.getLogger(__name__)


class PeerNotifier:
    """Reconcile pending mailboxes after sends, restarts and runner reconnects."""

    def __init__(
        self,
        store: PeerDeliveryStore,
        app_state: Any,
        conversations: Any,
        agent_store: Any,
        agent_cache: Any,
    ) -> None:
        self.store = store
        self.state = app_state
        self.conversations = conversations
        self.agents = agent_store
        self.cache = agent_cache

    def _spec(self, conv: Any) -> Any:
        from omnigent.runtime.workflow import _find_spec_by_name

        agent = self.agents.get(conv.agent_id) if conv.agent_id else None
        if agent is None:
            return None
        spec = self.cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        ).spec
        if conv.sub_agent_name:
            selected = _find_spec_by_name(spec, conv.sub_agent_name)
            if selected is None:
                raise ValueError("Notification sub-agent is unavailable")
            return selected
        return spec

    def defaults(self, conv: Any) -> DeliveryPolicy:
        """Agent YAML params supply defaults; session settings override them."""
        spec = self._spec(conv)
        return DeliveryPolicy.parse(spec.params.get("peer_messages")) if spec else DeliveryPolicy()

    def supported(self, conv: Any) -> bool:
        """Only adapters with a typed, acknowledged delivery path may start work."""
        from omnigent.inference_config import normalize_inference_harness

        spec = self._spec(conv)
        if spec is None:
            return False
        override = conv.harness_override
        harness = (
            override
            if override and override != "auto"
            else spec.executor.config.get("harness") or spec.executor.type
        )
        return normalize_inference_harness(harness) == "pi-native"

    async def _client(self, sid: str, conv: Any) -> Any:
        from omnigent.errors import ErrorCode, OmnigentError
        from omnigent.server.routes._sessions.helpers import _get_runner_client

        if self.state.runner_router is not None:
            try:
                return self.state.runner_router.client_for_session_resources(
                    sid, conversation=conv
                ).client
            except OmnigentError as exc:
                if exc.code == ErrorCode.WRONG_REPLICA:
                    raise
                return None
            except (LookupError, ConnectionError):
                return None
        return await _get_runner_client(sid, None, conversation=conv)

    async def run(self) -> None:
        """A database lease serializes each mailbox across backend processes."""
        slots = asyncio.Semaphore(4)

        async def one(workspace: int, sid: str, mailbox: str) -> None:
            async with slots:
                with workspace_scope(workspace):
                    try:
                        async with asyncio.timeout(120):
                            await self.deliver(sid, mailbox)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        _logger.exception("Peer notification deferred for session %s", sid)
                        await asyncio.to_thread(self.store.defer, sid, mailbox)

        while True:
            try:
                page = await asyncio.to_thread(self.store.candidates)
                await asyncio.gather(*(one(*row) for row in page))
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Peer notification scan failed")
            await asyncio.sleep(2)

    async def deliver(self, sid: str, mailbox: str) -> None:
        """Run as the current recipient owner, never as the sending agent."""
        from omnigent.errors import ErrorCode, OmnigentError
        from omnigent.server.routes._sessions.orchestration import (
            _ensure_runner_relay_ready,
            _ensure_runner_session_initialized,
            ensure_runner_connected,
        )

        conv = await asyncio.to_thread(self.conversations.get_conversation, sid)
        if conv is None or conv.archived:
            await asyncio.to_thread(self.store.defer, sid, mailbox, seconds=300)
            return
        if not await asyncio.to_thread(self.supported, conv):
            await asyncio.to_thread(self.store.defer, sid, mailbox, seconds=300)
            return
        default = await asyncio.to_thread(self.defaults, conv)
        policy = await asyncio.to_thread(self.store.policy, sid, mailbox, default)
        try:
            client = await self._client(sid, conv)
        except OmnigentError as exc:
            if exc.code == ErrorCode.WRONG_REPLICA:
                return
            raise
        if client is None and not policy.wake_on_message:
            await asyncio.to_thread(self.store.defer, sid, mailbox)
            return
        delivery = await asyncio.to_thread(self.store.claim, sid, mailbox, default)
        if delivery is None:
            return
        accepted = False
        try:
            owner = await asyncio.to_thread(
                self.conversations.get_session_owner_authority, sid, owner_only=True
            )
            if owner is None:
                return
            verifier = getattr(self.state, "peer_machine_verifier", None)
            if owner.user_id.startswith("oidc-machine:") and (
                verifier is None
                or not await asyncio.to_thread(verifier.principal_allowed, owner.user_id)
            ):
                return
            with account_authority_scope(owner.user_id, owner.generation):
                policy = await asyncio.to_thread(self.store.policy, sid, mailbox, default)
                if not policy.enabled:
                    return
                client = await self._client(sid, conv)
                if client is None and policy.wake_on_message:
                    client, conv = await ensure_runner_connected(
                        session_id=sid,
                        conv=conv,
                        app_state=self.state,
                        conversation_store=self.conversations,
                        runner_router=self.state.runner_router,
                    )
                if client is None:
                    return
                await _ensure_runner_session_initialized(
                    sid,
                    conv,
                    client,
                    self.conversations,
                    initializer=getattr(self.state, "runner_session_initializer", None),
                    require_success=True,
                )
                await _ensure_runner_relay_ready(
                    sid, conv.runner_id, client, self.conversations, conversation=conv
                )
                payload = {
                    "type": "peer_notification",
                    "notification_id": delivery.id,
                    "attempt_id": delivery.lease,
                    "message_ids": list(delivery.message_ids),
                }
                for _ in range(20):
                    response = await client.post(
                        f"/v1/sessions/{sid}/events", json=payload, timeout=10
                    )
                    accepted = (
                        response.status_code == 200 and response.json().get("delivered") is True
                    )
                    if accepted or response.status_code != 202:
                        break
                    await asyncio.sleep(0.5)

        finally:
            await asyncio.to_thread(self.store.finish, delivery, accepted=accepted)
