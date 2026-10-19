"""Server-owned attribution of inference aliases to the model actually routed."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping

PRICING_MODEL_LABEL_PREFIX = "omnigent.inference_pricing_model."


def pricing_model_label(alias: str) -> str:
    return PRICING_MODEL_LABEL_PREFIX + hashlib.sha256(alias.encode()).hexdigest()[:32]


def resolve_pricing_model(model: str, labels: Mapping[str, str]) -> str:
    return labels.get(pricing_model_label(model)) or model
