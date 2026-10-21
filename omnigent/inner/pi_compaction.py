"""Opt-in economic native Pi thresholds, independent of summarization budgets."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from copy import deepcopy

TARGETS_ENV = "OMNIGENT_PI_COMPACTION_TARGETS"
POLICY_FILE = "omnigent-compaction.json"
_DEFAULT_RESERVE = 16384
_DEFAULT_RECENT = 20000
_MAX_SAFE = 2**53 - 1


def _tokens(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= _MAX_SAFE:
        raise ValueError(f"{name} must be a non-negative safe integer")
    return value


def economic_settings(
    settings: Mapping[str, object],
    model_windows: Mapping[str, int],
    raw_targets: str | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    """Translate exact model targets to Pi settings and preserve original summary reserves.

    model_windows uses canonical provider/model keys. Environment targets use exact
    model IDs (possibly containing slashes); each generated provider gets its own
    override. Explicit global disable and tighter existing reserves remain honored.
    """
    raw = os.environ.get(TARGETS_ENV, "") if raw_targets is None else raw_targets
    result = deepcopy(dict(settings))
    if not raw.strip():
        return result, {}
    try:
        targets = json.loads(raw)
    except json.JSONDecodeError:
        raise ValueError(
            f"{TARGETS_ENV} must be a JSON object of exact model IDs to tokens"
        ) from None
    if not isinstance(targets, dict) or any(not isinstance(k, str) or not k for k in targets):
        raise ValueError(f"{TARGETS_ENV} must be a JSON object of exact model IDs to tokens")
    for key, value in targets.items():
        if _tokens(value, key) == 0:
            raise ValueError("Compaction targets must be positive")
    compaction = result.setdefault("compaction", {})
    if not isinstance(compaction, dict):
        raise ValueError("Pi compaction settings must be an object")
    ordinary_reserve = _tokens(compaction.get("reserveTokens", _DEFAULT_RESERVE), "reserveTokens")
    ordinary_recent = _tokens(
        compaction.get("keepRecentTokens", _DEFAULT_RECENT), "keepRecentTokens"
    )
    overrides = compaction.setdefault("modelOverrides", {})
    if not isinstance(overrides, dict):
        raise ValueError("Pi compaction modelOverrides must be an object")
    policy: dict[str, object] = {}
    for key, window in model_windows.items():
        _provider, model_id = key.split("/", 1)
        if model_id not in targets:
            continue
        target = targets[model_id]
        existing = overrides.get(key, {})
        if not isinstance(existing, dict):
            raise ValueError("Pi model compaction override must be an object")
        summary_reserve = _tokens(
            existing.get("reserveTokens", ordinary_reserve), "model reserveTokens"
        )
        recent = _tokens(
            existing.get("keepRecentTokens", ordinary_recent), "model keepRecentTokens"
        )
        if target >= window or window - target <= summary_reserve:
            continue  # Native/tighter limits already compact before this target.
        if target <= recent + summary_reserve:
            raise ValueError("Compaction target must leave room for retained context and summary")
        trigger_reserve = window - target
        overrides[key] = {**existing, "reserveTokens": trigger_reserve}
        policy[key] = {
            "triggerReserveTokens": trigger_reserve,
            "summaryReserveTokens": summary_reserve,
            "targetTokens": target,
        }
    return result, policy
