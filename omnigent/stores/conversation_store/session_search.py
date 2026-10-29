"""Relevance-ranked search across the sessions a caller may read."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import ColumnElement, and_, func, literal_column, or_, select, text
from sqlalchemy.orm import aliased

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationItem,
    SqlSessionPermission,
    current_workspace_id,
)
from omnigent.db.enum_codecs import ITEM_TYPE
from omnigent.db.group_authority import permission_principal_filter
from omnigent.db.utils import build_search_snippet, is_postgresql_family

SEARCHED_ITEM_TYPES = ("message", "compaction")
TEXT_SEARCH_CONFIG = "english"
INDEXED_TEXT_CHARS = 100_000
MAX_SESSIONS = 50
MAX_MATCHES_PER_SESSION = 3
_CANDIDATE_ITEMS = 400
_STATEMENT_TIMEOUT_MS = 15_000
_TERM = re.compile(r"\w+", re.UNICODE)
_OPERATORS = frozenset({"or", "and", "not"})


@dataclass(frozen=True)
class SessionSearchMatch:
    """One matching message inside a session or one of its sub-agent sessions."""

    item_id: str
    conversation_id: str
    created_at: int
    snippet: str


@dataclass(frozen=True)
class SessionSearchHit:
    """A top-level session that matched, with its best-ranked messages."""

    session_id: str
    title: str
    created_at: int
    updated_at: int
    score: float
    title_match: bool
    matches: list[SessionSearchMatch] = field(default_factory=list)


@dataclass(frozen=True)
class SearchPrincipal:
    """A grantee that must also be able to read every returned session."""

    user_id: str
    is_group: bool


def search_terms(query: str) -> list[str]:
    """Words from *query* used to place snippets, without search operators."""
    return [term for term in _TERM.findall(query) if term.lower() not in _OPERATORS]


def tsvector_expression(column: Any) -> ColumnElement[Any]:
    """The indexed text-search expression; queries must use it verbatim to hit the index."""
    return func.to_tsvector(
        literal_column(f"'{TEXT_SEARCH_CONFIG}'"),
        func.left(column, literal_column(str(INDEXED_TEXT_CHARS))),
    )


def _item_type_codes() -> list[Any]:
    return [literal_column(str(ITEM_TYPE[name])) for name in SEARCHED_ITEM_TYPES]


def _grant(root_id: Any, *criteria: ColumnElement[bool]) -> ColumnElement[bool]:
    return (
        select(SqlSessionPermission.conversation_id)
        .where(
            SqlSessionPermission.workspace_id == current_workspace_id(),
            SqlSessionPermission.conversation_id == root_id,
            *criteria,
        )
        .exists()
    )


def _audience_criteria(principal: SearchPrincipal) -> tuple[ColumnElement[bool], ...]:
    return (
        SqlSessionPermission.user_id == principal.user_id,
        SqlSessionPermission.is_group.is_(principal.is_group),
    )


def _snippet(search_text: str | None, terms: Sequence[str]) -> str:
    collapsed = " ".join((search_text or "").split())
    lowered = collapsed.lower()
    found = [(lowered.find(term.lower()), term) for term in terms]
    found = [(index, term) for index, term in found if index >= 0]
    if found:
        snippet = build_search_snippet(collapsed, min(found)[1])
        if snippet:
            return snippet
    return collapsed[:160] + ("…" if len(collapsed) > 160 else "")


class SessionSearch:
    """Run permission-filtered session search for a SQLAlchemy conversation store."""

    def __init__(self, store: Any) -> None:
        self._store = store

    def _split(self) -> bool:
        return self._store._conv_engine is not self._store._engine

    def _readable_roots(
        self, principals: tuple[str, ...], audience: Iterable[SearchPrincipal]
    ) -> set[str]:
        with self._store._session("search_sessions") as session:
            allowed = set(
                session.execute(
                    select(SqlSessionPermission.conversation_id).where(
                        SqlSessionPermission.workspace_id == current_workspace_id(),
                        permission_principal_filter(principals),
                    )
                ).scalars()
            )
            for principal in audience:
                if not allowed:
                    break
                allowed &= set(
                    session.execute(
                        select(SqlSessionPermission.conversation_id).where(
                            SqlSessionPermission.workspace_id == current_workspace_id(),
                            SqlSessionPermission.conversation_id.in_(allowed),
                            *_audience_criteria(principal),
                        )
                    ).scalars()
                )
            return allowed

    def search(
        self,
        query: str,
        principals: tuple[str, ...] | None,
        *,
        audience: Sequence[SearchPrincipal] = (),
        exclude_session_id: str | None = None,
        limit: int = 20,
    ) -> list[SessionSearchHit]:
        """
        Rank top-level sessions whose title or messages match *query*.

        A session is eligible when *principals* hold a direct or group grant on
        it (public links do not make sessions discoverable) and every
        *audience* principal holds the same kind of grant. Matches in sub-agent
        sessions count toward their top-level session.

        :param query: Web-search style text, e.g. ``'"warm pool" -staging'``.
        :param principals: The caller and its verified groups, from
            :func:`omnigent.db.group_authority.access_principals`, or ``None``
            on an unauthenticated single-user server.
        :param audience: Principals who must all be able to read each result.
        :param exclude_session_id: Top-level session to leave out of results.
        :param limit: Maximum sessions, at most :data:`MAX_SESSIONS`.
        :returns: Hits in descending relevance.
        """
        query = query.strip()
        if not query or principals == ():
            return []
        limit = max(1, min(limit, MAX_SESSIONS))
        terms = search_terms(query)
        if not terms:
            return []
        workspace = current_workspace_id()
        item_conv = aliased(SqlConversation)
        root = aliased(SqlConversation)

        root_filters: list[ColumnElement[bool]] = [
            root.workspace_id == workspace,
            root.parent_conversation_id.is_(None),
            root.archived.is_(False),
        ]
        if exclude_session_id is not None:
            root_filters.append(root.id != exclude_session_id)
        if principals is None:
            pass
        elif self._split():
            readable = self._readable_roots(principals, audience)
            if not readable:
                return []
            root_filters.append(root.id.in_(readable))
        else:
            root_filters.append(_grant(root.id, permission_principal_filter(principals)))
            root_filters.extend(
                _grant(root.id, *_audience_criteria(principal)) for principal in audience
            )

        with self._store._conv_session("search_sessions") as session:
            postgres = session.bind is not None and session.bind.dialect.name == "postgresql"
            if session.bind is not None and is_postgresql_family(session.bind.dialect.name):
                session.execute(
                    text(f"SET LOCAL statement_timeout = {int(_STATEMENT_TIMEOUT_MS)}")
                )

            if postgres:
                tsquery = func.websearch_to_tsquery(
                    literal_column(f"'{TEXT_SEARCH_CONFIG}'"), query
                )
                vector = tsvector_expression(SqlConversationItem.search_text)
                item_match = vector.op("@@")(tsquery)
                item_rank: ColumnElement[Any] = func.ts_rank_cd(vector, tsquery)
                title_vector = func.to_tsvector(
                    literal_column(f"'{TEXT_SEARCH_CONFIG}'"), root.title
                )
                title_match = title_vector.op("@@")(tsquery)
                title_rank: ColumnElement[Any] = func.ts_rank_cd(title_vector, tsquery)
            else:
                item_match = and_(
                    *(SqlConversationItem.search_text.ilike(f"%{term}%") for term in terms)
                )
                item_rank = cast(ColumnElement[Any], SqlConversationItem.created_at)
                title_match = and_(*(root.title.ilike(f"%{term}%") for term in terms))
                title_rank = cast(ColumnElement[Any], root.updated_at)

            items = session.execute(
                select(
                    SqlConversationItem.id,
                    SqlConversationItem.conversation_id,
                    SqlConversationItem.created_at,
                    root.id,
                    item_rank,
                )
                .join(
                    item_conv,
                    and_(
                        item_conv.workspace_id == SqlConversationItem.workspace_id,
                        item_conv.id == SqlConversationItem.conversation_id,
                    ),
                )
                .join(
                    root,
                    and_(
                        root.workspace_id == item_conv.workspace_id,
                        root.id == item_conv.root_conversation_id,
                    ),
                )
                .where(
                    SqlConversationItem.workspace_id == workspace,
                    SqlConversationItem.type.in_(_item_type_codes()),
                    item_match,
                    *root_filters,
                )
                .order_by(item_rank.desc())
                .limit(_CANDIDATE_ITEMS)
            ).all()
            titles = session.execute(
                select(root.id, title_rank)
                .where(title_match, *root_filters)
                .order_by(title_rank.desc())
                .limit(limit)
            ).all()

            scores: dict[str, float] = {}
            title_hits: set[str] = set()
            picked: dict[str, list[tuple[str, str, int]]] = {}
            for position, (item_id, conversation_id, created_at, root_id, _rank) in enumerate(
                items
            ):
                weight = 1.0 / (1 + position)
                scores[root_id] = scores.get(root_id, 0.0) + weight
                chosen = picked.setdefault(root_id, [])
                if len(chosen) < MAX_MATCHES_PER_SESSION:
                    chosen.append((item_id, conversation_id, created_at))
            for position, (root_id, _rank) in enumerate(titles):
                title_hits.add(root_id)
                scores[root_id] = scores.get(root_id, 0.0) + 2.0 / (1 + position)
            ranked = sorted(scores, key=lambda root_id: scores[root_id], reverse=True)[:limit]
            if not ranked:
                return []

            roots = {
                row.id: row
                for row in session.execute(
                    select(SqlConversation).where(
                        SqlConversation.workspace_id == workspace,
                        SqlConversation.id.in_(ranked),
                    )
                ).scalars()
            }
            wanted = [match for root_id in ranked for match in picked.get(root_id, [])]
            bodies: dict[tuple[str, str], str | None] = {}
            if wanted:
                bodies = {
                    (conversation_id, item_id): body
                    for item_id, conversation_id, body in session.execute(
                        select(
                            SqlConversationItem.id,
                            SqlConversationItem.conversation_id,
                            SqlConversationItem.search_text,
                        ).where(
                            SqlConversationItem.workspace_id == workspace,
                            or_(
                                *(
                                    and_(
                                        SqlConversationItem.conversation_id == conversation_id,
                                        SqlConversationItem.id == item_id,
                                    )
                                    for item_id, conversation_id, _ in wanted
                                )
                            ),
                        )
                    ).all()
                }

        hits = []
        for root_id in ranked:
            row = roots.get(root_id)
            if row is None:
                continue
            hits.append(
                SessionSearchHit(
                    session_id=root_id,
                    title=row.title,
                    created_at=row.created_at,
                    updated_at=row.updated_at,
                    score=round(scores[root_id], 6),
                    title_match=root_id in title_hits,
                    matches=[
                        SessionSearchMatch(
                            item_id=item_id,
                            conversation_id=conversation_id,
                            created_at=created_at,
                            snippet=_snippet(bodies.get((conversation_id, item_id)), terms),
                        )
                        for item_id, conversation_id, created_at in picked.get(root_id, [])
                    ],
                )
            )
        return hits
