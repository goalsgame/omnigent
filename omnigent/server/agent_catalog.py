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


def agent_catalog_presentation(config: Mapping[str, Any]) -> tuple[str | None, dict[str, str]]:
    """Validate operator labels and the default for new-session pickers."""
    allowed = agent_catalog_names(config)
    section = config.get("agents", {})
    default = section.get("default_name")
    labels = section.get("display_names", {})
    if default is not None and (
        not isinstance(default, str) or not default.strip() or default != default.strip()
    ):
        raise ValueError("agents.default_name must be a non-empty agent name")
    if not isinstance(labels, dict) or any(
        not isinstance(name, str)
        or not name.strip()
        or not isinstance(label, str)
        or not label.strip()
        for name, label in labels.items()
    ):
        raise ValueError("agents.display_names must map agent names to non-empty labels")
    if allowed is not None and (set(labels) | ({default} if default else set())) - allowed:
        raise ValueError("Agent presentation names must be included in agents.allowed_names")
    return default, labels
