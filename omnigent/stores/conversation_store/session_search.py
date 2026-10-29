"""Relevance-ranked search across the sessions a caller may read."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, cast

from sqlalchemy import ColumnElement, Float, and_, func, literal_column, or_, select, text
from sqlalchemy.orm import Session, aliased

from omnigent.db.db_models import (
    SqlConversation,
    SqlConversationItem,
    SqlSessionMemoryEntry,
    SqlSessionPermission,
    current_workspace_id,
)
from omnigent.db.enum_codecs import ITEM_TYPE
from omnigent.db.group_authority import permission_principal_filter
from omnigent.db.utils import build_search_snippet, is_postgresql_family
from omnigent.stores.conversation_store.session_memory import embedding_column, query_vector

SEARCHED_ITEM_TYPES = ("message", "compaction")
TEXT_SEARCH_CONFIG = "english"
INDEXED_TEXT_CHARS = 100_000
MAX_SESSIONS = 50
MAX_MATCHES_PER_SESSION = 3
_CANDIDATE_ITEMS = MAX_SESSIONS * MAX_MATCHES_PER_SESSION
_STATEMENT_TIMEOUT_MS = 15_000
_HEADLINE_OPTIONS = 'MaxFragments=1, MaxWords=30, MinWords=12, StartSel="", StopSel=""'
_TERM = re.compile(r"\w+", re.UNICODE)
_OPERATORS = frozenset({"or", "and", "not"})
_SYNTAX = re.compile(r'"|(^|\s)-\w|\bor\b', re.IGNORECASE)
MAX_MEMORIES_PER_SESSION = 3
_MEMORY_CANDIDATES = 100
_RRF_K = 10


@dataclass(frozen=True)
class SessionSearchMatch:
    """One matching message inside a session or one of its sub-agent sessions."""

    item_id: str
    conversation_id: str
    created_at: int
    snippet: str


@dataclass(frozen=True)
class SessionMemoryMatch:
    """A digest entry, such as a decision or open question, that matched."""

    kind: str
    text: str
    item_ids: list[str]
    created_at: int
    similarity: float | None


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
    memories: list[SessionMemoryMatch] = field(default_factory=list)


@dataclass(frozen=True)
class SearchPrincipal:
    """A grantee that must also be able to read every returned session."""

    user_id: str
    is_group: bool


def search_terms(query: str) -> list[str]:
    """Words from *query* used to place snippets, without search operators."""
    return [term for term in _TERM.findall(query) if term.lower() not in _OPERATORS]


def is_natural_language(query: str) -> bool:
    """Whether *query* uses no quoting, exclusion or ``or`` operators."""
    return _SYNTAX.search(query) is None


def tsvector_expression(column: Any) -> ColumnElement[Any]:
    """The indexed text-search expression; queries must use it verbatim to hit the index."""
    return func.to_tsvector(
        literal_column(f"'{TEXT_SEARCH_CONFIG}'"),
        func.left(column, literal_column(str(INDEXED_TEXT_CHARS))),
    )


def _contains(term: str) -> str:
    """A LIKE pattern matching *term* literally anywhere, escaped with a backslash."""
    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


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

    def _items(
        self,
        session: Session,
        root: Any,
        item_conv: Any,
        match: ColumnElement[bool],
        rank: ColumnElement[Any],
        root_filters: Sequence[ColumnElement[bool]],
    ) -> list[Any]:
        matched = (
            select(
                SqlConversationItem.id.label("item_id"),
                SqlConversationItem.conversation_id.label("conversation_id"),
                SqlConversationItem.created_at.label("created_at"),
                root.id.label("root_id"),
                rank.label("rank"),
                func.row_number()
                .over(partition_by=root.id, order_by=rank.desc())
                .label("per_session"),
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
                SqlConversationItem.workspace_id == current_workspace_id(),
                SqlConversationItem.type.in_(_item_type_codes()),
                match,
                *root_filters,
            )
            .subquery()
        )
        return list(
            session.execute(
                select(
                    matched.c.item_id,
                    matched.c.conversation_id,
                    matched.c.created_at,
                    matched.c.root_id,
                    matched.c.rank,
                )
                .where(matched.c.per_session <= MAX_MATCHES_PER_SESSION)
                .order_by(matched.c.rank.desc())
                .limit(_CANDIDATE_ITEMS)
            ).all()
        )

    def _memories(
        self,
        session: Session,
        root: Any,
        root_filters: Sequence[ColumnElement[bool]],
        terms: Sequence[str],
        any_term: ColumnElement[Any] | None,
        query_embedding: Sequence[float] | None,
        min_similarity: float,
    ) -> list[tuple[str, SessionMemoryMatch]]:
        entry = SqlSessionMemoryEntry
        base = (
            select(
                entry.id,
                entry.conversation_id,
                entry.kind,
                entry.text,
                entry.item_ids,
                entry.created_at,
            )
            .join(
                root,
                and_(root.workspace_id == entry.workspace_id, root.id == entry.conversation_id),
            )
            .where(entry.workspace_id == current_workspace_id(), *root_filters)
        )
        scores: dict[str, float] = {}
        rows: dict[str, Any] = {}
        similarities: dict[str, float] = {}
        if query_embedding is not None:
            distance = embedding_column().op("<=>", return_type=Float())(
                query_vector(query_embedding)
            )
            nearest = session.execute(
                base.add_columns(distance.label("distance"))
                .where(embedding_column().is_not(None))
                .order_by(distance)
                .limit(_MEMORY_CANDIDATES)
            ).all()
            for position, row in enumerate(nearest):
                score = 1.0 - float(row.distance)
                if score < min_similarity:
                    break
                rows[row.id] = row
                similarities[row.id] = score
                scores[row.id] = 1.0 / (_RRF_K + position)
        if terms:
            if any_term is not None:
                vector = func.to_tsvector(literal_column(f"'{TEXT_SEARCH_CONFIG}'"), entry.text)
                match: ColumnElement[bool] = vector.op("@@")(any_term)
                rank: ColumnElement[Any] = func.ts_rank_cd(vector, any_term)
            else:
                match = or_(*(entry.text.ilike(_contains(term), escape="\\") for term in terms))
                rank = cast(ColumnElement[Any], entry.created_at)
            matching = session.execute(
                base.where(match).order_by(rank.desc()).limit(_MEMORY_CANDIDATES)
            ).all()
            for position, row in enumerate(matching):
                rows.setdefault(row.id, row)
                scores[row.id] = scores.get(row.id, 0.0) + 1.0 / (_RRF_K + position)
        picked: list[tuple[str, SessionMemoryMatch]] = []
        per_session: dict[str, int] = {}
        for entry_id in sorted(scores, key=lambda key: scores[key], reverse=True):
            row = rows[entry_id]
            if per_session.get(row.conversation_id, 0) >= MAX_MEMORIES_PER_SESSION:
                continue
            per_session[row.conversation_id] = per_session.get(row.conversation_id, 0) + 1
            try:
                item_ids = [str(value) for value in json.loads(row.item_ids)]
            except (TypeError, ValueError):
                item_ids = []
            picked.append(
                (
                    row.conversation_id,
                    SessionMemoryMatch(
                        kind=row.kind,
                        text=row.text,
                        item_ids=item_ids,
                        created_at=row.created_at or 0,
                        similarity=(
                            round(similarities[entry_id], 4) if entry_id in similarities else None
                        ),
                    ),
                )
            )
        return picked

    def search(
        self,
        query: str,
        principals: tuple[str, ...] | None,
        *,
        audience: Sequence[SearchPrincipal] = (),
        exclude_session_id: str | None = None,
        limit: int = 20,
        query_embedding: Sequence[float] | None = None,
        min_similarity: float = 0.0,
    ) -> list[SessionSearchHit]:
        """
        Rank top-level sessions whose title, messages or digest match *query*.

        A session is eligible when *principals* hold a direct or group grant on
        it (public links do not make sessions discoverable) and every
        *audience* principal holds the same kind of grant. Matches in sub-agent
        sessions count toward their top-level session.

        A plain-language query that matches too few sessions with every word
        is widened to messages containing any of its words. Digest entries
        match by any word and, with *query_embedding*, by meaning.

        :param query: Web-search style text, e.g. ``'"warm pool" -staging'``,
            or a question such as ``"why did we pick gVisor"``.
        :param principals: The caller and its verified groups, from
            :func:`omnigent.db.group_authority.access_principals`, or ``None``
            on an unauthenticated single-user server.
        :param audience: Principals who must all be able to read each result.
        :param exclude_session_id: Top-level session to leave out of results.
        :param limit: Maximum sessions, at most :data:`MAX_SESSIONS`.
        :param query_embedding: Embedding of *query* for semantic matching of
            digest entries. PostgreSQL with session memory vectors only.
        :param min_similarity: Lowest cosine similarity a semantic match needs.
        :returns: Hits in descending relevance.
        """
        query = query.strip()
        if not query or principals == ():
            return []
        limit = max(1, min(limit, MAX_SESSIONS))
        terms = search_terms(query)
        if not terms and query_embedding is None:
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

            items: list[Any] = []
            titles: list[Any] = []
            headline_query: ColumnElement[Any] | None = None
            any_term: ColumnElement[Any] | None = None
            if terms and postgres:
                config = literal_column(f"'{TEXT_SEARCH_CONFIG}'")
                tsquery = func.websearch_to_tsquery(config, query)
                any_term = func.to_tsquery(config, " | ".join(terms))
                headline_query = tsquery
                vector = tsvector_expression(SqlConversationItem.search_text)
                items = self._items(
                    session,
                    root,
                    item_conv,
                    vector.op("@@")(tsquery),
                    func.ts_rank_cd(vector, tsquery),
                    root_filters,
                )
                if (
                    len(terms) > 1
                    and is_natural_language(query)
                    and len({row.root_id for row in items}) < limit
                ):
                    seen = {(row.conversation_id, row.item_id) for row in items}
                    widened = self._items(
                        session,
                        root,
                        item_conv,
                        vector.op("@@")(any_term),
                        func.ts_rank_cd(vector, any_term),
                        root_filters,
                    )
                    items += [
                        row for row in widened if (row.conversation_id, row.item_id) not in seen
                    ]
                    headline_query = any_term
                title_vector = func.to_tsvector(config, root.title)
                title_rank: ColumnElement[Any] = func.ts_rank_cd(title_vector, tsquery)
                titles = session.execute(
                    select(root.id, title_rank)
                    .where(title_vector.op("@@")(tsquery), *root_filters)
                    .order_by(title_rank.desc())
                    .limit(MAX_SESSIONS)
                ).all()
            elif terms:
                items = self._items(
                    session,
                    root,
                    item_conv,
                    and_(
                        *(
                            SqlConversationItem.search_text.ilike(_contains(term), escape="\\")
                            for term in terms
                        )
                    ),
                    cast(ColumnElement[Any], SqlConversationItem.created_at),
                    root_filters,
                )
                title_order = cast(ColumnElement[Any], root.updated_at)
                titles = session.execute(
                    select(root.id, title_order)
                    .where(
                        and_(*(root.title.ilike(_contains(term), escape="\\") for term in terms)),
                        *root_filters,
                    )
                    .order_by(title_order.desc())
                    .limit(MAX_SESSIONS)
                ).all()
            memories = self._memories(
                session,
                root,
                root_filters,
                terms,
                any_term,
                query_embedding if postgres else None,
                min_similarity,
            )

            scores: dict[str, float] = {}
            title_hits: set[str] = set()
            picked: dict[str, list[tuple[str, str, int]]] = {}
            per_session: dict[str, int] = {}
            position = 0
            for item_id, conversation_id, created_at, root_id, _rank in items:
                if per_session.get(root_id, 0) >= MAX_MATCHES_PER_SESSION:
                    continue
                per_session[root_id] = per_session.get(root_id, 0) + 1
                scores[root_id] = scores.get(root_id, 0.0) + 1.0 / (1 + position)
                position += 1
                picked.setdefault(root_id, []).append((item_id, conversation_id, created_at))
            for position, (root_id, _rank) in enumerate(titles):
                title_hits.add(root_id)
                scores[root_id] = scores.get(root_id, 0.0) + 2.0 / (1 + position)
            remembered: dict[str, list[SessionMemoryMatch]] = {}
            for root_id, memory in memories:
                if root_id not in remembered:
                    scores[root_id] = scores.get(root_id, 0.0) + 3.0 / (1 + len(remembered))
                remembered.setdefault(root_id, []).append(memory)
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
                            func.ts_headline(
                                literal_column(f"'{TEXT_SEARCH_CONFIG}'"),
                                func.left(
                                    SqlConversationItem.search_text,
                                    literal_column(str(INDEXED_TEXT_CHARS)),
                                ),
                                headline_query,
                                literal_column(f"'{_HEADLINE_OPTIONS}'"),
                            )
                            if headline_query is not None
                            else SqlConversationItem.search_text,
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
                            snippet=_snippet(
                                bodies.get((conversation_id, item_id)),
                                [] if postgres else terms,
                            ),
                        )
                        for item_id, conversation_id, created_at in picked.get(root_id, [])
                    ],
                    memories=remembered.get(root_id, []),
                )
            )
        return hits
