"""Validation for credential-free TLS Lore repository URLs."""

from urllib.parse import urlsplit


def lore_remote(value: str) -> str:
    """Validate a credential-free TLS Lore URL without changing its repository path."""
    parts = urlsplit(value)
    if (
        parts.scheme != "lores"
        or not parts.hostname
        or parts.username is not None
        or parts.password is not None
        or parts.query
        or parts.fragment
        or "%" in value
        or "\\" in value
        or parts.path in ("", "/")
        or any(c.isspace() or ord(c) < 32 for c in value)
        or any(segment in (".", "..", "") for segment in parts.path[1:].split("/"))
    ):
        raise ValueError("Expected a credential-free lores://host/repository URL")
    _ = parts.port
    return value
