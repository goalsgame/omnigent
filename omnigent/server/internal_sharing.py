"""Deployment-configured verified OIDC audience for read-only internal sharing."""

import os

from omnigent.db.group_authority import group_principal
from omnigent.server.auth import AuthProvider, UnifiedAuthProvider


def internal_sharing_group(auth_provider: AuthProvider | None) -> str | None:
    name = os.environ.get("OMNIGENT_INTERNAL_SHARING_GROUP")
    if name is None:
        return None
    # Misconfiguration must never silently become a broader authenticated audience.
    group_principal(name)
    if (
        not isinstance(auth_provider, UnifiedAuthProvider)
        or not auth_provider.supports_group_sharing
    ):
        raise RuntimeError("Internal sharing requires verified OIDC group authentication")
    return name
