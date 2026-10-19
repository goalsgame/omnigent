"""Server-owned attribution of inference aliases to the model actually routed."""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping

PRICING_MODEL_LABEL_PREFIX = "omnigent.inference_pricing_model."


def pricing_model_label(alias: str) -> str:
    return PRICING_MODEL_LABEL_PREFIX + hashlib.sha256(alias.encode()).hexdigest()[:32]


def resolve_pricing_model(model: str, labels: Mapping[str, str]) -> str:
    return labels.get(pricing_model_label(model)) or model


def reject_pricing_label_writes(keys: Iterable[str]) -> None:
    """Pricing attribution is writable only by the authenticated inference proxy."""
    from omnigent.errors import ErrorCode, OmnigentError

    if any(key.startswith(PRICING_MODEL_LABEL_PREFIX) for key in keys):
        raise OmnigentError(
            "Inference pricing labels are server-internal", code=ErrorCode.INVALID_INPUT
        )
