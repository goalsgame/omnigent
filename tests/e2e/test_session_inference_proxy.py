"""Run a credential-free host/runner turn through the real TLS inference proxy."""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import yaml
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

from omnigent.host.connect import HARNESS_CREDENTIAL_ENV_VARS
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    poll_session_until_terminal,
    send_user_message_to_session,
    set_mock_served_models,
    upload_agent,
)
from tests.e2e.helpers import final_assistant_text
from tests.e2e.test_host_e2e import _wait_for_host_online


def _certificate(root: Path) -> None:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    (root / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    (root / "key.pem").chmod(0o600)
    (root / "cert.pem").write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_opted_in_host_launches_runner_and_streams_without_provider_credentials(
    tmp_path: Path,
    unused_tcp_port: int,
    isolated_mock_llm_server_url: str,
) -> None:
    root = Path(__file__).resolve().parents[2]
    _certificate(tmp_path)
    # Trust the ephemeral CA in child processes only, including commands that
    # deliberately ignore ambient proxy/TLS environment configuration.
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "sitecustomize.py").write_text(
        "import certifi\ncertifi.where=lambda:" + repr(str(tmp_path / "cert.pem")) + "\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in HARNESS_CREDENTIAL_ENV_VARS
        and not k.startswith(("OPENAI_", "OPENROUTER_", "ANTHROPIC_", "HARNESS_", "DATABRICKS_"))
    }
    env.update(
        {
            "PYTHONPATH": os.pathsep.join((str(trust), str(root))),
            "OMNIGENT_E2E_TLS_CERT": str(tmp_path / "cert.pem"),
            "SSL_CERT_FILE": str(tmp_path / "cert.pem"),
            "OMNIGENT_SKIP_WEB_UI": "true",
            "OMNIGENT_HOST_NO_OPEN": "1",
        }
    )
    host_root = tmp_path / "host"
    host_root.mkdir()
    host_id = uuid.uuid4().hex
    host_config = host_root / "config.yaml"
    host_config.write_text(
        yaml.safe_dump(
            {
                "host": {"host_id": host_id, "name": "proxy-host"},
                # A developer's local Ollama must not make this credential-free host ready.
                "dismissed_detections": ["ollama"],
            }
        )
    )
    host_env = {
        **env,
        "OMNIGENT_CONFIG": str(host_config),
        "OMNIGENT_CONFIG_HOME": str(host_root),
        "OMNIGENT_DATA_DIR": str(host_root),
        "OMNIGENT_SERVER_INFERENCE": "1",
    }
    server_env = {
        **env,
        "OMNIGENT_E2E_ROOT": str(tmp_path),
        "OMNIGENT_E2E_PORT": str(unused_tcp_port),
        "OMNIGENT_E2E_PROVIDER": isolated_mock_llm_server_url,
        "OMNIGENT_E2E_SIGNING_KEY": "s" * 32,
        "OMNIGENT_DATA_DIR": str(tmp_path / "server-data"),
    }
    marker = "INFERENCE_PROXY_HOST_TURN_OK"
    set_mock_served_models(isolated_mock_llm_server_url, ["test-model"])
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": marker}])
    base = f"https://127.0.0.1:{unused_tcp_port}"
    processes = []
    session_id = None
    with (
        (tmp_path / "server.log").open("w") as server_log,
        (tmp_path / "host.log").open("w") as host_log,
    ):
        server = subprocess.Popen(
            [sys.executable, "-m", "tests.e2e._session_inference_proxy_server"],
            cwd=root,
            env=server_env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        processes.append(server)
        try:
            with httpx.Client(
                base_url=base,
                verify=ssl.create_default_context(cafile=str(tmp_path / "cert.pem")),
                timeout=30,
                trust_env=False,
            ) as client:
                deadline = time.monotonic() + 60
                while True:
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    assert server.poll() is None, (tmp_path / "server.log").read_text()
                    assert time.monotonic() < deadline, "server did not start"
                    time.sleep(0.1)
                host = subprocess.Popen(
                    [sys.executable, "-m", "omnigent.host._daemon_entry", "--server", base],
                    cwd=root,
                    env=host_env,
                    stdout=host_log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(host)
                _wait_for_host_online(client, host_id, timeout=60)
                agent = tmp_path / "proxy-agent"
                agent.mkdir()
                (agent / "proxy-agent.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "name": "proxy-agent",
                            "executor": {"harness": "openai-agents", "model": "test-model"},
                            "prompt": "Reply briefly without using tools.",
                            "os_env": {"type": "caller_process", "cwd": str(tmp_path)},
                        }
                    )
                )
                try:
                    agent_id = lookup_agent_id(client, upload_agent(client, agent))
                except httpx.HTTPStatusError as exc:
                    raise AssertionError(exc.response.text) from exc
                created = client.post(
                    "/v1/sessions",
                    json={"agent_id": agent_id, "host_id": host_id, "workspace": str(tmp_path)},
                )
                created.raise_for_status()
                session_id = created.json()["id"]
                runner_id = created.json()["runner_id"]
                assert runner_id
                response_id = send_user_message_to_session(
                    client, session_id=session_id, content="Reply briefly."
                )
                result = poll_session_until_terminal(
                    client, session_id=session_id, response_id=response_id, timeout=120
                )
                assert result["status"] == "completed", result
                assert marker in final_assistant_text(result)
                ledger = client.get("/v1/_e2e/proxy_requests").json()
                assert ledger["calls"] and all(call["authorized"] for call in ledger["calls"])
                assert ledger["active"] == 0
                assert "server-only-provider" not in "".join(
                    path.read_text() for path in host_root.rglob("*.json")
                )
                client.post(f"/v1/sessions/{session_id}/stop")
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
