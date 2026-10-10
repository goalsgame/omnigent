"""Request-scoped group authority derived from verified OIDC claims."""

from __future__ import annotations

import base64
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass

from omnigent.db.db_models import current_workspace_id

GROUP_PRINCIPAL_PREFIX = "oidc-group:"
# Leave room for the rest of the JWT and cookie within a 4 KiB browser limit.
MAX_GROUP_CLAIM_BYTES = 1536


def group_principal(name: str) -> str:
    """Encode an exact issuer group name into the permission row's 128-byte key."""
    try:
        encoded = name.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("Group names must contain valid Unicode") from exc
    if not name or name != name.strip() or len(encoded) > 87:
        raise ValueError("Group names must be nonempty, unpadded and at most 87 UTF-8 bytes")
    if any(ord(c) < 32 or ord(c) == 127 for c in name):
        raise ValueError("Group names cannot contain control characters")
    return GROUP_PRINCIPAL_PREFIX + base64.urlsafe_b64encode(encoded).decode().rstrip("=")


def group_name(principal: str) -> str | None:
    if not principal.startswith(GROUP_PRINCIPAL_PREFIX):
        return None
    encoded = principal[len(GROUP_PRINCIPAL_PREFIX) :]
    try:
        name = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        return name if group_principal(name) == principal else None
    except (ValueError, UnicodeError):
        return None


def verified_groups(value: object) -> tuple[str, ...]:
    """Malformed claims confer no authority; strings are never split or normalized."""
    if not isinstance(value, list) or len(value) > 256:
        return ()
    try:
        if any(not isinstance(item, str) for item in value):
            return ()
        names = sorted(set(value))
        if len(json.dumps(names, separators=(",", ":")).encode("ascii")) > MAX_GROUP_CLAIM_BYTES:
            return ()
        return tuple(sorted({group_principal(item) for item in names}))
    except ValueError:
        return ()


@dataclass(frozen=True)
class GroupAuthority:
    user_id: str
    workspace_id: int
    principals: tuple[str, ...]
    expires_at: int


_authority: ContextVar[GroupAuthority | None] = ContextVar("group_authority", default=None)


@contextmanager
def group_authority_scope() -> Iterator[None]:
    """Start each app request without authority inherited from another app."""
    token = _authority.set(None)
    try:
        yield
    finally:
        _authority.reset(token)


def clear_group_authority() -> None:
    _authority.set(None)


def bind_group_authority(user_id: str, groups: object, expires_at: object) -> None:
    clear_group_authority()
    if isinstance(expires_at, int) and not isinstance(expires_at, bool):
        _authority.set(
            GroupAuthority(user_id, current_workspace_id(), verified_groups(groups), expires_at)
        )


def access_principals(user_id: str | None) -> tuple[str, ...]:
    """Only the authenticated subject in this workspace inherits group grants."""
    if user_id is None:
        return ()
    authority = _authority.get()
    if (
        authority
        and authority.user_id == user_id
        and authority.workspace_id == current_workspace_id()
        and authority.expires_at > time.time()
    ):
        return (user_id, *authority.principals)
    return (user_id,)


def group_snapshot(user_id: str) -> dict[str, object] | None:
    authority = _authority.get()
    if len(access_principals(user_id)) <= 1 or authority is None:
        return None
    return {
        "groups": [group_name(principal) for principal in authority.principals],
        "expires_at": authority.expires_at,
    }
