"""Machine Cloud opt-in and reader-visible operator authorization status."""

import asyncio

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field

from omnigent.server.auth import AuthProvider
from omnigent.server.google_cloud_machine import GoogleCloudMachineBroker
from omnigent.server.permissions import check_session_access
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.conversation_store import ConversationStore
from omnigent.stores.permission_store import PermissionStore


class MachineCloudDecision(BaseModel):
    enabled: bool = Field(strict=True)


def create_machine_cloud_router(
    broker: GoogleCloudMachineBroker,
    auth: AuthProvider,
    permissions: PermissionStore,
    conversations: ConversationStore,
) -> APIRouter:
    router = APIRouter()

    @router.get("/sessions/{session_id}/google-cloud")
    async def status(session_id: str, request: Request, response: Response):
        response.headers["Cache-Control"] = "no-store"
        user = require_user(request, auth)
        if user is None:
            raise HTTPException(401, "Authentication required")
        if not await asyncio.to_thread(
            check_session_access, user, session_id, 1, permissions, conversations
        ):
            raise HTTPException(403, "Session access required")
        grants = await asyncio.to_thread(permissions.list_for_sessions, [session_id])
        owners = [p.user_id for p in grants.get(session_id, []) if p.level == 4]
        if len(owners) != 1:
            raise HTTPException(403, "Unique machine session owner required")
        try:
            return await asyncio.to_thread(broker.session, session_id, owners[0])
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None

    @router.post("/sessions/{session_id}/google-cloud")
    async def decide(
        session_id: str, body: MachineCloudDecision, request: Request, response: Response
    ):
        response.headers["Cache-Control"] = "no-store"
        user = require_user(request, auth)
        if user is None:
            raise HTTPException(401, "Authentication required")
        authority = await asyncio.to_thread(auth.get_machine_credential_user_id, request)
        if authority is None or authority != user:
            raise HTTPException(403, "Direct machine authentication required for Cloud opt-in")
        try:
            return await asyncio.to_thread(broker.session, session_id, user, enabled=body.enabled)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from None

    return router
