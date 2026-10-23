"""Complete a Claude Code or Codex turn on a credential-free host through the inference proxy."""

from __future__ import annotations

import os
import shutil
import ssl
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.host.connect import HARNESS_CREDENTIAL_ENV_VARS
from tests.e2e.conftest import (
    configure_mock_llm,
    lookup_agent_id,
    set_mock_served_models,
    upload_agent,
)
from tests.e2e.test_host_e2e import _wait_for_host_online
from tests.e2e.test_session_inference_proxy import _certificate

_CLI = {"claude-native": "claude", "codex-native": "codex"}
_MODEL = {"claude-native": "anthropic/claude-test", "codex-native": "openai/gpt-test"}

pytestmark = pytest.mark.timeout(300)


def _trusting_interpreter(root: Path, cert: Path) -> Path:
    """A venv overlay whose interpreter trusts ``cert``, for the token helper Codex runs."""
    import sysconfig

    real = Path(sysconfig.get_path("purelib"))
    overlay = root / "trusting-python"
    site_dir = overlay / real.relative_to(sys.prefix)
    site_dir.mkdir(parents=True)
    (overlay / "bin").mkdir()
    (overlay / "pyvenv.cfg").write_text((Path(sys.prefix) / "pyvenv.cfg").read_text())
    (site_dir / "trust-test-ca.pth").write_text(
        f"import site; site.addsitedir({str(real)!r}); import certifi; "
        f"certifi.where = lambda: {str(cert)!r}\n"
    )
    python = overlay / "bin" / "python3"
    python.symlink_to(sys.executable)
    return python


def _assistant_text(snapshot: dict) -> str:
    texts = []
    for item in snapshot.get("items") or []:
        data = item.get("data") if isinstance(item.get("data"), dict) else item
        if item.get("type") == "message" and data.get("role") == "assistant":
            for part in data.get("content") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return "\n".join(texts)


@pytest.mark.parametrize("harness", ["claude-native", "codex-native"])
def test_native_cli_turn_uses_server_inference_without_provider_credentials(
    tmp_path: Path, unused_tcp_port: int, isolated_mock_llm_server_url: str, harness: str
) -> None:
    if shutil.which(_CLI[harness]) is None or shutil.which("tmux") is None:
        pytest.skip(f"requires the {_CLI[harness]} CLI and tmux")
    root = Path(__file__).resolve().parents[2]
    _certificate(tmp_path)
    trust = tmp_path / "trust"
    trust.mkdir()
    (trust / "sitecustomize.py").write_text(
        "import certifi\ncertifi.where=lambda:" + repr(str(tmp_path / "cert.pem")) + "\n"
    )
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in HARNESS_CREDENTIAL_ENV_VARS
        and not k.startswith(
            (
                "OPENAI_",
                "OPENROUTER_",
                "ANTHROPIC_",
                "HARNESS_",
                "DATABRICKS_",
                "CLAUDE_",
                "CODEX_",
            )
        )
    }
    env.update(
        {
            "PYTHONPATH": os.pathsep.join((str(trust), str(root))),
            "OMNIGENT_E2E_TLS_CERT": str(tmp_path / "cert.pem"),
            "SSL_CERT_FILE": str(tmp_path / "cert.pem"),
            "NODE_EXTRA_CA_CERTS": str(tmp_path / "cert.pem"),
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
                "host": {"host_id": host_id, "name": "native-proxy-host"},
                "dismissed_detections": ["ollama"],
            }
        )
    )
    host_env = {
        **env,
        "HOME": str(host_root),
        "USERPROFILE": str(host_root),
        "XDG_CONFIG_HOME": str(host_root / ".config"),
        "CODEX_HOME": str(host_root / ".codex"),
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
        "OMNIGENT_E2E_NATIVE": harness,
        "OMNIGENT_DATA_DIR": str(tmp_path / "server-data"),
    }
    marker = "NATIVE_PROXY_TURN_OK"
    set_mock_served_models(isolated_mock_llm_server_url, [_MODEL[harness]])
    configure_mock_llm(isolated_mock_llm_server_url, [{"text": marker} for _ in range(8)])
    base = f"https://127.0.0.1:{unused_tcp_port}"
    processes: list[subprocess.Popen] = []
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
                    [
                        str(_trusting_interpreter(tmp_path, tmp_path / "cert.pem")),
                        "-m",
                        "omnigent.host._daemon_entry",
                        "--server",
                        base,
                    ],
                    cwd=root,
                    env=host_env,
                    stdout=host_log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(host)
                _wait_for_host_online(client, host_id, timeout=60)
                agent = tmp_path / "native-agent"
                agent.mkdir()
                (agent / "native-agent.yaml").write_text(
                    yaml.safe_dump(
                        {
                            "name": "native-agent",
                            "executor": {"harness": harness},
                            "prompt": "Reply briefly without using tools.",
                            "os_env": {"type": "caller_process", "cwd": str(tmp_path)},
                        }
                    )
                )
                agent_id = lookup_agent_id(client, upload_agent(client, agent))
                created = client.post(
                    "/v1/sessions",
                    json={"agent_id": agent_id, "host_id": host_id, "workspace": str(tmp_path)},
                )
                assert created.status_code == 201, created.text
                session_id = created.json()["id"]
                runner_id = created.json()["runner_id"]
                assert runner_id
                deadline = time.monotonic() + 60
                while (
                    client.get(f"/v1/runners/{runner_id}/status").json().get("online") is not True
                ):
                    assert time.monotonic() < deadline, "runner did not come online"
                    time.sleep(0.2)
                initialized = client.post(
                    f"/v1/_e2e/sessions/{session_id}/initialize", timeout=60
                ).json()
                assert initialized["status"] == 201, initialized
                assert initialized["payload"]["inference_config_verified"] is True
                posted = client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={
                        "type": "message",
                        "data": {
                            "role": "user",
                            "content": [{"type": "input_text", "text": "Reply briefly."}],
                        },
                    },
                    timeout=60,
                )
                assert posted.status_code < 400, posted.text
                deadline = time.monotonic() + 180
                while marker not in _assistant_text(
                    client.get(f"/v1/sessions/{session_id}").json()
                ):
                    assert time.monotonic() < deadline, "no proxied assistant reply"
                    time.sleep(1)
                ledger = client.get("/v1/_e2e/proxy_requests").json()
                assert ledger["calls"], ledger
                assert all(call["authorized"] for call in ledger["calls"])
                assert all(call["model"] == _MODEL[harness] for call in ledger["calls"])
                assert "server-only-provider" not in "".join(
                    path.read_text(errors="ignore") for path in host_root.rglob("*.json")
                )
        except BaseException:
            for name in ("server.log", "host.log"):
                sys.stderr.write(f"--- {name}\n{(tmp_path / name).read_text()[-6000:]}\n")
            raise
        finally:
            for process in reversed(processes):
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
