"""OIDC machine session -> launch-token broker -> authenticated private Git clone.

The CLI server, RSA verification, ownership, launcher and generated preparation
command are real. Kubernetes, OIDC/GitHub endpoints and the credential cipher
use local stand-ins. No LLM, cloud resources or real credentials are required.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import httpx
import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from omnigent.host.identity import HOST_ID_ENV_VAR, HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER
from tests._helpers.live_server import find_free_port
from tests.e2e._fake_github_https import FakeGitHub, make_bare_repo
from tests.e2e._github_machine_bootstrap import TOKEN
from tests.e2e.test_managed_clone_shared_token_survives_no_provider_e2e import (
    _CLONE_URL,
    _ORG_REPO,
    _POD_HOME_DIR,
    _await_capture,
    _init_container_env,
    _prepare_pod_home,
    _spawn_server,
    _wait_for_health,
    _write_image_git_identity,
    _write_server_config,
)
from tests.server.test_oidc_machine_auth import CLIENT, ISSUER, PRINCIPAL, SUBJECT, signed_token

pytestmark = pytest.mark.timeout(600)


def test_machine_managed_session_clones_with_scoped_installation_token(tmp_path: Path) -> None:
    port = find_free_port()
    config_path = _write_server_config(tmp_path, port)
    config = yaml.safe_load(config_path.read_text())
    config.update(
        {
            "oidc_machine_auth": {
                "audience": "agent-api",
                "role_client": "web",
                "role": "automation",
                "clients": {CLIENT: SUBJECT},
            },
            "github_machine_auth": {
                PRINCIPAL: {"installation_id": 123, "repository_ids": [456], "access": "write"},
            },
        }
    )
    config_path.write_text(yaml.safe_dump(config))
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    (tmp_path / "app.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="key-one", alg="RS256", use="sig")
    (tmp_path / "jwks.json").write_text(json.dumps({"keys": [jwk]}))
    env = {
        "GITHUB_MACHINE_FIXTURES": str(tmp_path),
        "OMNIGENT_AUTH_ENABLED": "1",
        "OMNIGENT_LOCAL_SINGLE_USER": "0",
        "OMNIGENT_AUTH_PROVIDER": "oidc",
        "OMNIGENT_OIDC_ISSUER": ISSUER,
        "OMNIGENT_OIDC_CLIENT_ID": "web",
        "OMNIGENT_OIDC_CLIENT_SECRET": "test-only",
        "OMNIGENT_OIDC_REDIRECT_URI": "https://app.example.test/auth/callback",
        "OMNIGENT_OIDC_COOKIE_SECRET": "63" * 32,
        "OMNIGENT_GITHUB_APP_ID": "123",
        "OMNIGENT_GITHUB_APP_CLIENT_ID": "test-client",
        "OMNIGENT_GITHUB_APP_CLIENT_SECRET": "test-only",
        "OMNIGENT_GITHUB_APP_PRIVATE_KEY_PATH": str(tmp_path / "app.pem"),
        "OMNIGENT_GITHUB_APP_REDIRECT_URI": "https://app.example.test/v1/connections/github/callback",
        "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
    }
    capture_path = tmp_path / "submitted.json"
    proc, log_path = _spawn_server(
        tmp_path,
        config_path,
        port,
        capture_path,
        extra_env=env,
        bootstrap="from tests.e2e._github_machine_bootstrap import main; main()",
    )
    try:
        base_url = f"http://127.0.0.1:{port}"
        _wait_for_health(proc, base_url, log_path)
        headers = {"Authorization": f"Bearer {signed_token(key)}"}
        agents_response = httpx.get(f"{base_url}/v1/agents", headers=headers, timeout=10)
        assert agents_response.status_code == 200, agents_response.text
        agents = agents_response.json()["data"]
        response = httpx.post(
            f"{base_url}/v1/sessions",
            headers=headers,
            json={
                "agent_id": agents[0]["id"],
                "host_type": "managed",
                "workspace": _CLONE_URL,
            },
            timeout=120,
        )
        assert response.status_code == 201, response.text
        secret = _await_capture(capture_path, "create_namespaced_secret", log_path)["manifest"]
        job = _await_capture(capture_path, "create_namespaced_job", log_path)["manifest"]
        pod = job["spec"]["template"]["spec"]
        prep_container = pod["initContainers"][0]
        command = prep_container["command"]
        pod_home = _prepare_pod_home(tmp_path)
        github_root = tmp_path / "github"
        make_bare_repo(github_root, _ORG_REPO)
        with FakeGitHub(github_root, "x-access-token", TOKEN) as github:
            prep_env = _init_container_env(
                prep_container,
                secret["stringData"],
                pod_home=pod_home,
                system_cfg=_write_image_git_identity(tmp_path),
                proxy_url=github.proxy_url,
            )
            # Only the launch token can obtain credentials; no shared or human token exists.
            prep_env.pop("GIT_TOKEN", None)
            prep = subprocess.run(
                [*command[:-1], command[-1].replace(_POD_HOME_DIR, str(pod_home))],
                env=prep_env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert prep.returncode == 0, prep.stdout + prep.stderr + log_path.read_text()[-2000:]
            assert github.saw_authenticated_request
        assert (pod_home / "workspace" / "private-widget" / "README.md").is_file()
        assert json.loads((tmp_path / "installation-request.json").read_text())[
            "repository_ids"
        ] == [456]
        # The generated launch credential is bound to the machine, not a generic account.
        host_id = next(
            e["value"] for e in pod["containers"][0]["env"] if e["name"] == HOST_ID_ENV_VAR
        )
        probe = httpx.get(
            f"{base_url}/v1/hosts/{host_id}/credentials/github",
            headers={MANAGED_HOST_TOKEN_HEADER: secret["stringData"][HOST_TOKEN_ENV_VAR]},
            timeout=10,
        )
        assert probe.status_code == 200, probe.text
        assert probe.json()["owner"] == PRINCIPAL
        assert probe.json()["token"] == TOKEN
    finally:
        proc.kill()
        proc.wait(timeout=30)
