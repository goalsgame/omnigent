"""Exact, cached public OpenRouter prices when the shared catalog cannot resolve a model."""

from __future__ import annotations

import math
import os
import threading
from typing import Any

import httpx
from cachetools import TTLCache

_cache: TTLCache[str, dict[str, dict[str, Any]]] = TTLCache(maxsize=1, ttl=300)
_lock = threading.Lock()


def _catalog() -> dict[str, dict[str, Any]]:
    with _lock:
        if "models" not in _cache:
            try:
                with httpx.Client(timeout=3.0, trust_env=False) as client:
                    response = client.get("https://openrouter.ai/api/v1/models")
                    response.raise_for_status()
                    data = response.json()
                rows = data.get("data") if isinstance(data, dict) else None
                if not isinstance(rows, list):
                    raise ValueError("Invalid model catalog")
                _cache["models"] = {
                    row["id"]: row["pricing"]
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("id"), str)
                    and isinstance(row.get("pricing"), dict)
                }
            except (httpx.HTTPError, ValueError):
                # Cache failures too: an outage must not delay every usage event.
                _cache["models"] = {}
        return _cache["models"]


def model_rates(model: str) -> dict[str, float] | None:
    """Return finite per-token rates for an exact id, or leave it unpriced."""
    if os.environ.get("OMNIGENT_DISABLE_CATALOG_LOOKUP") == "1" or "/" not in model:
        return None
    raw = _catalog().get(model)
    if not raw or raw.get("overrides"):
        # Context-tier pricing needs per-request usage, not cumulative tokens.
        return None
    result: dict[str, float] = {}
    for key in ("prompt", "completion", "input_cache_read", "input_cache_write"):
        value = raw.get(key)
        if value is None:
            if key in ("prompt", "completion"):
                return None
            continue
        if isinstance(value, bool):
            return None
        try:
            rate = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(rate) or rate < 0:
            return None
        result[key] = rate
    return result
