"""Session memory: digests of decisions and discussions, embedded for semantic search."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.db.db_models import workspace_scope
from omnigent.server.inference_upstreams import GoogleInferenceCredential, vertex_base_url
from omnigent.stores.conversation_store.session_memory import (
    MemoryEntry,
    SessionDigestRecord,
    TranscriptItem,
)

_logger = logging.getLogger(__name__)

DIGEST_KINDS = ("decision", "discussion", "open_question", "tangent")
MAX_TRANSCRIPT_CHARS = 400_000
MAX_ENTRIES = 30
LEASE_SECONDS = 600
_BATCH = 5
_EMBED_BATCH = 32
_QUERY_EMBED_TIMEOUT_SECONDS = 5
_MAX_BACKOFF_SECONDS = 86_400
_MODEL = re.compile(r"[A-Za-z0-9._@-]+")
_FIELD_CHARS = {"title": 200, "text": 2_000, "rationale": 1_000, "summary": 2_000}

DIGEST_INSTRUCTIONS = """\
You maintain the shared memory of a team's working sessions with AI agents. \
Read the session and record what a teammate would want to find later.

Return JSON with:
- summary: two to four sentences on the session's goal and outcome.
- topics: up to eight short topic phrases.
- entries: the notable content. Each entry has a kind:
  - decision: something chosen or agreed. Put what was decided in text, why in \
rationale, and the options considered and rejected in alternatives.
  - discussion: a substantive exploration of a question or design, with the \
positions taken and any conclusion.
  - open_question: something raised and still unresolved at the end of the messages.
  - tangent: a side topic that came up but was not the session's main goal.
  Give each entry a short title, and in refs the [n] numbers of the messages it \
comes from.

Skip routine progress, tool output and small talk. Record only what the messages \
support. Write in the language of the session. Prefer a few specific entries to \
many vague ones, and return at most 30.

When a previous digest is given, the messages continue that session. Return the \
complete updated digest: keep entries that still hold, revise those the new \
messages change (an answered open question becomes a decision), and list in from \
the ids of previous entries that an entry carries forward."""

DIGEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "topics": {"type": "array", "items": {"type": "string"}},
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": list(DIGEST_KINDS)},
                    "title": {"type": "string"},
                    "text": {"type": "string"},
                    "rationale": {"type": "string"},
                    "alternatives": {"type": "array", "items": {"type": "string"}},
                    "refs": {"type": "array", "items": {"type": "integer"}},
                    "from": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["kind", "title", "text", "refs"],
            },
        },
    },
    "required": ["summary", "topics", "entries"],
}
# Not responseSchema: it makes Gemini 3 pad JSON with whitespace up to the token limit.
DIGEST_PROMPT = (
    DIGEST_INSTRUCTIONS
    + "\n\nRespond with only a JSON object matching this JSON Schema:\n"
    + json.dumps(DIGEST_SCHEMA)
)


@dataclass(frozen=True)
class VertexModel:
    """A Google model served by Vertex AI."""

    project: str
    location: str
    model: str

    @classmethod
    def parse(cls, raw: Any, name: str, extra: frozenset[str] = frozenset()) -> VertexModel:
        keys = {"project", "location", "model"}
        if not isinstance(raw, dict) or not keys <= set(raw) or set(raw) - keys - extra:
            raise ValueError(f"session_memory.{name} requires project, location and model")
        model = raw["model"]
        if not isinstance(model, str) or _MODEL.fullmatch(model) is None:
            raise ValueError(f"Invalid session_memory.{name}.model")
        vertex_base_url(raw["project"], raw["location"])
        return cls(raw["project"], raw["location"], model)

    def url(self, method: str) -> str:
        base = vertex_base_url(self.project, self.location)
        return f"{base}/publishers/google/models/{self.model}:{method}"


@dataclass(frozen=True)
class SessionMemoryConfig:
    """Operator configuration for session digests and embeddings."""

    digest: VertexModel
    embeddings: VertexModel
    dimensions: int = 768
    idle_minutes: int = 15
    min_similarity: float = 0.6

    @classmethod
    def parse(cls, raw: Any) -> SessionMemoryConfig | None:
        """Validate the ``session_memory`` server config block at startup."""
        if raw is None:
            return None
        allowed = {"digest", "embeddings", "idle_minutes", "min_similarity"}
        if (
            not isinstance(raw, dict)
            or set(raw) - allowed
            or not {"digest", "embeddings"} <= set(raw)
        ):
            raise ValueError("session_memory requires digest and embeddings")
        embeddings = raw["embeddings"]
        dimensions = embeddings.get("dimensions", 768) if isinstance(embeddings, dict) else None
        if type(dimensions) is not int or not 1 <= dimensions <= 2000:
            raise ValueError("session_memory.embeddings.dimensions must be between 1 and 2000")
        idle = raw.get("idle_minutes", 15)
        if type(idle) is not int or idle < 1:
            raise ValueError("session_memory.idle_minutes must be a positive integer")
        threshold = raw.get("min_similarity", 0.6)
        if type(threshold) not in (int, float) or not 0 <= threshold < 1:
            raise ValueError("session_memory.min_similarity must be at least 0 and below 1")
        return cls(
            digest=VertexModel.parse(raw["digest"], "digest"),
            embeddings=VertexModel.parse(embeddings, "embeddings", frozenset({"dimensions"})),
            dimensions=dimensions,
            idle_minutes=idle,
            min_similarity=float(threshold),
        )


def _clip(value: Any, limit: int) -> str:
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _strings(value: Any, count: int, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [clipped for clipped in (_clip(item, limit) for item in value[:count]) if clipped]


def render_transcript(
    title: str, previous: dict[str, Any] | None, items: Sequence[TranscriptItem]
) -> str:
    """The digest request body: title, any previous digest, and numbered messages."""
    parts = [f"Session title: {title or '(untitled)'}"]
    if previous:
        carried = {
            "summary": previous.get("summary", ""),
            "topics": previous.get("topics", []),
            "entries": [
                {
                    key: entry[key]
                    for key in ("kind", "title", "text", "rationale", "alternatives")
                    if key in entry
                }
                | {"id": f"p{index}"}
                for index, entry in enumerate(previous.get("entries", []), start=1)
            ],
        }
        parts.append("Previous digest:\n" + json.dumps(carried, ensure_ascii=False))
    parts.append("Messages:")
    parts.extend(
        f"[{number}] {item.role}: {item.text}" for number, item in enumerate(items, start=1)
    )
    return "\n\n".join(parts)


def build_digest(
    raw: Any,
    previous: dict[str, Any] | None,
    items: Sequence[TranscriptItem],
    now: int,
) -> tuple[dict[str, Any], list[MemoryEntry]]:
    """
    Validate a model's digest and resolve its message references.

    :param raw: The decoded JSON the model returned.
    :param previous: The digest the model was asked to update, if any.
    :param items: The numbered messages the model saw.
    :param now: Timestamp for entries without dated sources.
    :returns: The stored digest and its searchable entries.
    :raises ValueError: When *raw* is not a digest.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("entries"), list):
        raise ValueError("Digest response is not a digest object")
    earlier = previous.get("entries", []) if previous else []
    dates = {item.item_id: item.created_at for item in items}
    summary = _clip(raw.get("summary"), _FIELD_CHARS["summary"])
    topics = _strings(raw.get("topics"), 8, 80)
    stored: list[dict[str, Any]] = []
    for value in raw["entries"][:MAX_ENTRIES]:
        if not isinstance(value, dict) or value.get("kind") not in DIGEST_KINDS:
            continue
        title = _clip(value.get("title"), _FIELD_CHARS["title"])
        body = _clip(value.get("text"), _FIELD_CHARS["text"])
        if not title and not body:
            continue
        item_ids: list[str] = []
        message_ids: list[str] = []
        created_at = 0
        for source in value.get("from") or []:
            match = re.fullmatch(r"p(\d+)", source) if isinstance(source, str) else None
            if match and 1 <= int(match.group(1)) <= len(earlier):
                carried = earlier[int(match.group(1)) - 1]
                item_ids.extend(carried.get("item_ids", []))
                message_ids.extend(carried.get("message_ids", []))
                created_at = max(created_at, int(carried.get("created_at", 0)))
        for ref in value.get("refs") or []:
            if type(ref) is int and 1 <= ref <= len(items):
                item = items[ref - 1]
                item_ids.append(item.item_id)
                if item.role != "summary" and item.message_id:
                    message_ids.append(item.message_id)
                created_at = max(created_at, item.created_at)
        if not item_ids:
            continue
        entry: dict[str, Any] = {
            "kind": value["kind"],
            "title": title,
            "text": body,
            "item_ids": list(dict.fromkeys(item_ids)),
            "message_ids": list(dict.fromkeys(message_ids)),
            "created_at": created_at or now,
        }
        rationale = _clip(value.get("rationale"), _FIELD_CHARS["rationale"])
        if rationale:
            entry["rationale"] = rationale
        alternatives = _strings(value.get("alternatives"), 8, 300)
        if alternatives:
            entry["alternatives"] = alternatives
        stored.append(entry)
    digest = {"summary": summary, "topics": topics, "entries": stored}
    memories = []
    if summary:
        lines = [summary] + ([f"Topics: {', '.join(topics)}"] if topics else [])
        memories.append(
            MemoryEntry(kind="summary", text="\n".join(lines), created_at=max(dates.values()))
        )
    for entry in stored:
        lines = [f"{entry['title']}: {entry['text']}" if entry["title"] else entry["text"]]
        if "rationale" in entry:
            lines.append(f"Why: {entry['rationale']}")
        if "alternatives" in entry:
            lines.append(f"Rejected: {'; '.join(entry['alternatives'])}")
        memories.append(
            MemoryEntry(
                kind=entry["kind"],
                text="\n".join(lines),
                item_ids=entry["item_ids"],
                created_at=entry["created_at"],
            )
        )
    return digest, memories


class SessionMemory:
    """Writes digests for quiet sessions and embeds them for search."""

    def __init__(
        self,
        config: SessionMemoryConfig,
        conversations: Any,
        *,
        credential: Any = None,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self._conversations = conversations
        self._credential = credential or GoogleInferenceCredential()
        self._client = client or httpx.AsyncClient(timeout=httpx.Timeout(180, connect=10))
        self._clock = clock
        self._vectors = False

    async def prepare(self) -> None:
        """Create vector storage; until then only digests are written."""
        await asyncio.to_thread(
            self._conversations.ensure_session_memory_vectors,
            self.config.dimensions,
            self.config.embeddings.model,
        )
        self._vectors = True

    @property
    def store(self) -> Any:
        return self._conversations.session_memory()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post(self, url: str, body: dict[str, Any]) -> Any:
        token = await self._credential.credential()
        response = await self._client.post(
            url, json=body, headers={"Authorization": f"Bearer {token}"}
        )
        if response.status_code != 200:
            raise RuntimeError(f"Vertex AI returned HTTP {response.status_code}")
        return response.json()

    async def embed(self, text: str, task: str) -> list[float]:
        """Embed *text* for *task*, ``"RETRIEVAL_DOCUMENT"`` or ``"RETRIEVAL_QUERY"``."""
        data = await self._post(
            self.config.embeddings.url("embedContent"),
            {
                "content": {"parts": [{"text": text}]},
                "taskType": task,
                "outputDimensionality": self.config.dimensions,
                "autoTruncate": True,
            },
        )
        try:
            values = data["embedding"]["values"]
        except (KeyError, TypeError):
            raise RuntimeError("Vertex AI returned no embedding") from None
        if not isinstance(values, list) or len(values) != self.config.dimensions:
            raise RuntimeError("Vertex AI returned an embedding of the wrong size")
        return [float(value) for value in values]

    async def embed_query(self, query: str) -> list[float] | None:
        """Embed a search query, or ``None`` so search continues on text alone."""
        if not self._vectors:
            return None
        try:
            async with asyncio.timeout(_QUERY_EMBED_TIMEOUT_SECONDS):
                return await self.embed(query, "RETRIEVAL_QUERY")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — search falls back to text matching
            _logger.warning("Session search continues without semantic matching: %s", exc)
            return None

    async def write_digest(
        self,
        title: str,
        previous: dict[str, Any] | None,
        items: Sequence[TranscriptItem],
        now: int,
    ) -> tuple[dict[str, Any], list[MemoryEntry]]:
        """Ask the digest model to describe *items* and validate its answer."""
        data = await self._post(
            self.config.digest.url("generateContent"),
            {
                "systemInstruction": {"parts": [{"text": DIGEST_PROMPT}]},
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": render_transcript(title, previous, items)}],
                    }
                ],
                "generationConfig": {
                    "maxOutputTokens": 16_384,
                    "responseMimeType": "application/json",
                },
            },
        )
        try:
            candidate = data["candidates"][0]
            text = "".join(
                part.get("text", "")
                for part in candidate["content"]["parts"]
                if not part.get("thought")
            )
        except (KeyError, IndexError, TypeError, AttributeError):
            raise RuntimeError("Digest model returned no content") from None
        if candidate.get("finishReason") not in (None, "STOP"):
            raise RuntimeError(f"Digest model stopped early: {candidate.get('finishReason')}")
        text = text.strip()
        if text.startswith("```"):
            text = text.strip("`").removeprefix("json").strip()
        try:
            raw = json.loads(text)
        except ValueError:
            raise RuntimeError("Digest model returned invalid JSON") from None
        return build_digest(raw, previous, items, now)

    async def process(self, session_id: str) -> bool:
        """
        Bring one session's digest up to date in the current workspace.

        :returns: Whether the session was claimed.
        """
        now = int(self._clock())
        store = self.store
        claimed: SessionDigestRecord | None = await asyncio.to_thread(
            store.claim, session_id, now, LEASE_SECONDS
        )
        if claimed is None:
            return False
        try:
            state = await asyncio.to_thread(store.session_state, session_id)
            if state is None:
                return True
            title, updated_at = state
            items, more = await asyncio.to_thread(
                store.transcript, session_id, claimed.items_through, MAX_TRANSCRIPT_CHARS
            )
            if not items:
                await asyncio.to_thread(
                    store.advance,
                    session_id,
                    items_through=claimed.items_through,
                    source_updated_at=updated_at,
                    now=now,
                )
                return True
            digest, entries = await self.write_digest(title, claimed.digest, items, now)
            await asyncio.to_thread(
                store.save,
                session_id,
                digest=digest,
                entries=entries,
                items_through=items[-1].position,
                source_updated_at=claimed.source_updated_at if more else updated_at,
                model=self.config.digest.model,
                now=int(self._clock()),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — recorded on the digest and retried later
            _logger.warning("Session digest failed for %s: %s", session_id, exc)
            delay = min(_MAX_BACKOFF_SECONDS, 300 * 2 ** min(claimed.attempts, 10))
            await asyncio.to_thread(
                store.fail,
                session_id,
                str(exc) or type(exc).__name__,
                now=now,
                retry_at=now + delay,
            )
        return True

    async def embed_backlog(self) -> int:
        """Embed entries that have no embedding yet; returns how many were stored."""
        store = self.store
        pending = await asyncio.to_thread(
            store.claim_unembedded, int(self._clock()), LEASE_SECONDS, _EMBED_BATCH
        )
        done = 0
        for workspace, entry_id, text in pending:
            try:
                values = await self.embed(text, "RETRIEVAL_DOCUMENT")
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — retried on the next pass
                _logger.warning("Session memory embedding deferred: %s", exc)
                break
            await asyncio.to_thread(store.set_embedding, workspace, entry_id, values)
            done += 1
        return done

    async def tick(self) -> bool:
        """Process quiet sessions and pending embeddings; returns whether work remains."""
        now = int(self._clock())
        rows = await asyncio.to_thread(
            self.store.candidates, now, self.config.idle_minutes * 60, _BATCH
        )
        for workspace, session_id in rows:
            with workspace_scope(workspace):
                try:
                    await self.process(session_id)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    _logger.exception("Session digest pass failed for %s", session_id)
        embedded = await self.embed_backlog() if self._vectors else 0
        return len(rows) == _BATCH or embedded == _EMBED_BATCH

    async def run(self) -> None:
        """Keep digests and embeddings current until cancelled."""
        while True:
            busy = False
            try:
                busy = await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception("Session memory pass failed")
            await asyncio.sleep(2 if busy else 60)
