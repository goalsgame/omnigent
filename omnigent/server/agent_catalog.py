"""Operator-configured names available for agent discovery and new sessions."""

from collections.abc import Mapping
from typing import Any


def agent_catalog_names(config: Mapping[str, Any]) -> frozenset[str] | None:
    """Read agents.allowed_names; omission preserves the unrestricted catalog."""
    section = config.get("agents", {})
    if not isinstance(section, Mapping):
        raise ValueError("agents must be a mapping")
    names = section.get("allowed_names")
    if names is None:
        return None
    if not isinstance(names, list) or any(
        not isinstance(name, str) or not name.strip() or name != name.strip() for name in names
    ):
        raise ValueError("agents.allowed_names must be a list of non-empty agent names")
    return frozenset(names)
