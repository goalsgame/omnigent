"""Local external-service stand-ins for the managed machine clone journey."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import httpx
import jwt
from cryptography.hazmat.primitives import serialization

from omnigent.server.github_app_client import GitHubAppClient
from tests.server.test_oidc_machine_auth import ISSUER

TOKEN = "ghs_local_installation_fixture"


def main() -> None:
    """Boot the real CLI; replace only OIDC/GitHub network and credential storage."""
    fixtures = Path(os.environ["GITHUB_MACHINE_FIXTURES"])
    public_key = serialization.load_pem_private_key(
        (fixtures / "app.pem").read_bytes(), password=None
    ).public_key()
    jwks = (fixtures / "jwks.json").read_bytes()

    def discovery(url, **kwargs):
        assert url == ISSUER + "/.well-known/openid-configuration"
        return httpx.Response(
            200,
            request=httpx.Request("GET", url),
            json={
                "issuer": ISSUER,
                "authorization_endpoint": ISSUER + "/authorize",
                "token_endpoint": ISSUER + "/token",
                "jwks_uri": ISSUER + "/certs",
            },
        )

    def fetch_jwks(request, **kwargs):
        assert request.full_url == ISSUER + "/certs"
        return BytesIO(jwks)

    def installation(request):
        assert request.method == "POST"
        assert str(request.url) == "https://api.github.com/app/installations/123/access_tokens"
        claims = jwt.decode(
            request.headers["Authorization"].removeprefix("Bearer "),
            public_key,
            algorithms=["RS256"],
            options={"verify_aud": False},
        )
        assert claims["iss"] == "123"
        scope = json.loads(request.content)
        assert scope == {
            "repository_ids": [456],
            "permissions": {"contents": "write", "pull_requests": "write", "metadata": "read"},
        }
        (fixtures / "installation-request.json").write_text(json.dumps(scope))
        return httpx.Response(
            201,
            json={
                "token": TOKEN,
                "expires_at": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            },
        )

    with (
        patch("httpx.get", discovery),
        patch("jwt.jwks_client.urllib.request.urlopen", fetch_jwks),
        patch.object(
            GitHubAppClient,
            "_http_client",
            lambda self: httpx.AsyncClient(
                transport=httpx.MockTransport(installation),
                timeout=15,
            ),
        ),
    ):
        from omnigent.cli import main as cli_main

        cli_main()
