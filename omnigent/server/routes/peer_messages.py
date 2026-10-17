"""Permission-checked, pull-based communication between independent sessions."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from fastapi.routing import APIRoute
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.types import Message, Receive, Scope, Send

from omnigent.db.account_authority import account_generation
from omnigent.db.utils import now_epoch_us
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores import ConversationStore
from omnigent.stores.peer_message_store import PeerMessage, PeerMessageStore
from omnigent.stores.permission_store import PermissionStore

_ID = r"^[a-f0-9]{32}$"
_RETENTION_US = 90 * 86400 * 1_000_000


class SendPeerMessage(BaseModel):
    """Sender session and user are derived from the authenticated endpoint."""

    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=_ID)
    recipient_session_id: str = Field(pattern=_ID)
    subject: str = Field(min_length=1, max_length=256)
    body: str = Field(min_length=1, max_length=8192)
    reply_to: str | None = Field(default=None, pattern=_ID)

    @field_validator("body", "subject")
    @classmethod
    def bounded_text(cls, value: str) -> str:
        """Keep Unicode content within the stored and returned byte budget."""
        if len(value.encode("utf-8")) > 8192:
            raise ValueError("Message body exceeds 8192 UTF-8 bytes")
        return value


def create_peer_messages_router(
    store: PeerMessageStore,
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth: AuthProvider | None,
) -> APIRouter:
    """Expose inboxes to readers, sends and acknowledgements to session editors."""

    async def authorize(request: Request, session_id: str, level: int) -> tuple[str, str | None]:
        user = require_user(request, auth) or "local"
        try:
            if uuid.UUID(hex=session_id).hex != session_id:
                raise ValueError("Noncanonical session id")
        except ValueError as exc:
            raise HTTPException(404, "Session not found") from exc
        mailbox_id = await asyncio.to_thread(store.mailbox_id, session_id)
        await require_access(user, session_id, level, permissions, conversations)
        if await asyncio.to_thread(conversations.get_conversation, session_id) is None:
            raise HTTPException(404, "Session not found")
        if mailbox_id is None and level >= 2:
            mailbox_id = await asyncio.to_thread(store.initialize_mailbox_id, session_id, user)
        return user, mailbox_id

    class BoundedMessageRoute(APIRoute):
        async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
            if scope["type"] == "http" and scope["method"] == "POST":
                request = Request(scope)
                require_user(request, auth)
                require_trusted_origin(request)
                size = 0
                original_receive = receive

                async def bounded_receive() -> Message:
                    nonlocal size
                    message = await original_receive()
                    if message["type"] == "http.request":
                        size += len(message.get("body", b""))
                        if size > 65536:
                            raise HTTPException(413, "Message request exceeds 64 KiB")
                    return message

                receive = bounded_receive
            await super().handle(scope, receive, send)

    def private_response(response: Response) -> None:
        response.headers["Cache-Control"] = "private, no-store"

    router = APIRouter(route_class=BoundedMessageRoute, dependencies=[Depends(private_response)])

    @router.post("/sessions/{session_id}/peer-messages", status_code=201)
    async def send_message(session_id: str, message: SendPeerMessage, request: Request) -> dict:
        user, sender_mailbox = await authorize(request, session_id, 2)
        _, recipient_mailbox = await authorize(request, message.recipient_session_id, 2)
        assert sender_mailbox is not None and recipient_mailbox is not None
        if session_id == message.recipient_session_id:
            raise HTTPException(422, "Peer messages require a different recipient session")
        now = now_epoch_us()
        result = await asyncio.to_thread(
            store.send,
            PeerMessage(
                id=message.id,
                sender_session_id=session_id,
                recipient_session_id=message.recipient_session_id,
                sender_user_id=user,
                sender_generation=account_generation(user),
                sender_mailbox_id=sender_mailbox,
                recipient_mailbox_id=recipient_mailbox,
                subject=message.subject,
                body=message.body,
                reply_to=message.reply_to,
                created_at=now,
                expires_at=now + _RETENTION_US,
            ),
        )
        return asdict(result)

    @router.get("/sessions/{session_id}/peer-messages")
    async def inbox(
        session_id: str,
        request: Request,
        direction: Literal["received", "sent"] = "received",
        unread_only: bool = False,
        cursor: str | None = Query(default=None, max_length=64),
        limit: int = Query(default=10, ge=1, le=20),
    ) -> dict:
        _, mailbox_id = await authorize(request, session_id, 1)
        after = None
        if cursor:
            try:
                timestamp, identifier = cursor.split(":")
                after = (int(timestamp), uuid.UUID(hex=identifier).hex)
                if not 0 <= after[0] <= 2**63 - 1 or after[1] != identifier:
                    raise ValueError("Noncanonical cursor")
            except ValueError as exc:
                raise HTTPException(422, "Invalid message cursor") from exc
        if mailbox_id is None:
            return {"messages": [], "cursor": None, "has_more": False}
        rows = await asyncio.to_thread(
            store.list_messages,
            session_id,
            sent=direction == "sent",
            unread_only=unread_only,
            after=after,
            limit=limit + 1,
            mailbox_id=mailbox_id,
        )
        page = rows[:limit]
        return {
            "messages": [asdict(row) for row in page],
            "cursor": f"{page[-1].created_at}:{page[-1].id}" if len(rows) > limit else None,
            "has_more": len(rows) > limit,
        }

    @router.post("/sessions/{session_id}/peer-messages/{message_id}/acknowledge")
    async def acknowledge(session_id: str, message_id: str, request: Request) -> dict:
        user, mailbox_id = await authorize(request, session_id, 2)
        try:
            if uuid.UUID(hex=message_id).hex != message_id:
                raise ValueError("Noncanonical message id")
        except ValueError as exc:
            raise HTTPException(404, "Message not found") from exc
        if not await asyncio.to_thread(
            store.acknowledge, session_id, message_id, user, mailbox_id=mailbox_id
        ):
            raise HTTPException(404, "Message not found")
        return {"id": message_id, "acknowledged": True}

    return router
