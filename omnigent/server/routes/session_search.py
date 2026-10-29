"""Ranked search across the sessions the caller may read."""

from __future__ import annotations

import asyncio
import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Query, Request

from omnigent.db.group_authority import access_principals
from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import require_access, require_user
from omnigent.stores import ConversationStore
from omnigent.stores.conversation_store.session_search import (
    MAX_SESSIONS,
    SearchPrincipal,
    SessionSearchHit,
)
from omnigent.stores.permission_store import PermissionStore

if TYPE_CHECKING:
    from omnigent.server.session_memory import SessionMemory

_SESSION_ID = re.compile(r"^[a-f0-9]{32}$")
_MAX_QUERY_CHARS = 500


def _hit(hit: SessionSearchHit) -> dict[str, Any]:
    return {
        "session_id": hit.session_id,
        "title": hit.title,
        "created_at": hit.created_at,
        "updated_at": hit.updated_at,
        "score": hit.score,
        "title_match": hit.title_match,
        "matches": [
            {
                "item_id": match.item_id,
                "conversation_id": match.conversation_id,
                "created_at": match.created_at,
                "snippet": match.snippet,
            }
            for match in hit.matches
        ],
        "memories": [
            {
                "kind": memory.kind,
                "text": memory.text,
                "item_ids": memory.item_ids,
                "created_at": memory.created_at,
                "similarity": memory.similarity,
            }
            for memory in hit.memories
        ],
    }


def _query(q: str) -> str:
    query = q.strip()
    if not query:
        raise OmnigentError("Search query must not be empty", code=ErrorCode.INVALID_INPUT)
    return query


def create_session_search_router(
    conversations: ConversationStore,
    permissions: PermissionStore | None,
    auth: AuthProvider | None,
    memory: SessionMemory | None = None,
) -> APIRouter:
    """Expose session search, the audience-limited agent search and session digests."""
    router = APIRouter()

    async def semantic(query: str) -> dict[str, Any]:
        if memory is None:
            return {}
        return {
            "query_embedding": await memory.embed_query(query),
            "min_similarity": memory.config.min_similarity,
        }

    def root_of(session_id: str) -> str:
        current = conversations.get_conversation(session_id)
        seen: set[str] = set()
        while current is not None and current.parent_conversation_id is not None:
            if current.id in seen:
                break
            seen.add(current.id)
            current = conversations.get_conversation(current.parent_conversation_id)
        if current is None:
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        return current.id

    def audience(root: str) -> list[SearchPrincipal]:
        if permissions is None:
            return []
        result: list[SearchPrincipal] = []
        after = None
        while True:
            page, after = permissions.list_for_session(root, limit=1000, after_user_id=after)
            result.extend(SearchPrincipal(grant.user_id, grant.is_group) for grant in page)
            if after is None:
                return result

    def principals(user: str | None) -> tuple[str, ...] | None:
        return None if user is None else access_principals(user)

    @router.get("/sessions/search")
    async def search_sessions(
        request: Request,
        q: str = Query(..., max_length=_MAX_QUERY_CHARS),
        limit: int = Query(20, ge=1, le=MAX_SESSIONS),
    ) -> dict[str, Any]:
        """Rank the caller's readable sessions by title and message text."""
        user = require_user(request, auth)
        query = _query(q)
        hits = await asyncio.to_thread(
            conversations.search_sessions,
            query,
            principals(user),
            limit=limit,
            **await semantic(query),
        )
        return {"object": "list", "data": [_hit(hit) for hit in hits], "has_more": False}

    @router.get("/sessions/{session_id}/search")
    async def search_for_session(
        request: Request,
        session_id: str,
        q: str = Query(..., max_length=_MAX_QUERY_CHARS),
        limit: int = Query(10, ge=1, le=MAX_SESSIONS),
    ) -> dict[str, Any]:
        """
        Search on behalf of an agent in *session_id*.

        Results are limited to sessions every principal granted on this
        session can also read, since all of them can read the agent's output.
        """
        user = require_user(request, auth)
        if not _SESSION_ID.fullmatch(session_id):
            raise OmnigentError("Session not found", code=ErrorCode.NOT_FOUND)
        await require_access(user, session_id, LEVEL_READ, permissions, conversations)
        query = _query(q)
        root = await asyncio.to_thread(root_of, session_id)
        members = await asyncio.to_thread(audience, root)
        hits = await asyncio.to_thread(
            conversations.search_sessions,
            query,
            principals(user),
            audience=members,
            exclude_session_id=root,
            limit=limit,
            **await semantic(query),
        )
        return {"object": "list", "data": [_hit(hit) for hit in hits], "has_more": False}

    @router.get("/sessions/{session_id}/digest")
    async def get_session_digest(request: Request, session_id: str) -> dict[str, Any]:
        """
        The decisions, discussions, open questions and tangents of a session.

        Sub-agent sessions return their top-level session's digest. Message
        references in ``item_ids`` belong to the top-level session.
        """
        user = require_user(request, auth)
        if memory is None or not _SESSION_ID.fullmatch(session_id):
            raise OmnigentError("Session digest not found", code=ErrorCode.NOT_FOUND)
        await require_access(user, session_id, LEVEL_READ, permissions, conversations)
        root = await asyncio.to_thread(root_of, session_id)
        record = await asyncio.to_thread(memory.store.get, root)
        digest = (record.digest if record is not None else None) or {}
        return {
            "object": "session.digest",
            "session_id": root,
            "status": record.status if record is not None else "pending",
            "summary": digest.get("summary", ""),
            "topics": digest.get("topics", []),
            "entries": digest.get("entries", []),
            "model": record.model if record is not None else None,
            "updated_at": record.updated_at if record is not None else None,
            "error": record.error if record is not None and record.status == "failed" else None,
        }

    return router
