"""Internal audience configuration must fail closed and use exact OIDC groups."""

from unittest.mock import Mock

import pytest

from omnigent.server.auth import UnifiedAuthProvider
from omnigent.server.internal_sharing import internal_sharing_group


def test_unconfigured_preserves_existing_deployments(monkeypatch):
    monkeypatch.delenv("OMNIGENT_INTERNAL_SHARING_GROUP", raising=False)
    assert internal_sharing_group(None) is None


@pytest.mark.parametrize("name", ["", " Employees", "Employees ", "line\nbreak", "x" * 88])
def test_invalid_internal_audience_is_rejected(monkeypatch, name):
    monkeypatch.setenv("OMNIGENT_INTERNAL_SHARING_GROUP", name)
    with pytest.raises(ValueError):
        internal_sharing_group(None)


def test_internal_audience_requires_oidc_not_email_suffix_or_any_login(monkeypatch):
    monkeypatch.setenv("OMNIGENT_INTERNAL_SHARING_GROUP", "employees")
    with pytest.raises(RuntimeError, match="verified OIDC"):
        internal_sharing_group(None)
    provider = Mock(spec=UnifiedAuthProvider)
    provider.supports_group_sharing = False
    with pytest.raises(RuntimeError, match="verified OIDC"):
        internal_sharing_group(provider)
    provider.supports_group_sharing = True
    assert internal_sharing_group(provider) == "employees"
