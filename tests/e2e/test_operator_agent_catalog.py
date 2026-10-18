"""Real HTTP server catalog filtering across an existing-database upgrade."""

import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml


def test_operator_catalog_survives_server_restart(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[2]
    specs = tmp_path / "agents"
    specs.mkdir()
    for name in ("custom-pi", "reviewer"):
        (specs / f"{name}.yaml").write_text(
            f"name: {name}\nexecutor:\n  harness: pi-native\nprompt: Help the team.\n"
        )
    config = tmp_path / "config.yaml"
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "PYTHONPATH": str(root),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'server.db'}",
        "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
        "ARTIFACT_DIR": str(tmp_path / "artifacts"),
        "OMNIGENT_AUTH_ENABLED": "0",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "OMNIGENT_CONFIG": str(config),
        "OMNIGENT_BUILTIN_AGENT_DIRS": os.pathsep.join(
            str(p) for p in sorted(specs.glob("*.yaml"))
        ),
    }
    catalogs = []
    configured = {
        "agents": {
            "allowed_names": ["custom-pi", "reviewer"],
            "display_names": {"custom-pi": "Team Assistant", "reviewer": "Code Reviewer"},
            "default_name": "custom-pi",
        }
    }
    for settings in [{}, configured]:
        config.write_text(yaml.safe_dump(settings))
        with (tmp_path / "server.log").open("a") as log:
            proc = subprocess.Popen(
                [sys.executable, str(root / "deploy/docker/entrypoint.py")],
                env=env,
                stdout=log,
                stderr=log,
            )
            try:
                deadline = time.monotonic() + 60
                with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=2) as client:
                    while True:
                        assert proc.poll() is None, (tmp_path / "server.log").read_text()
                        try:
                            response = client.get("/health")
                            if response.status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        assert time.monotonic() < deadline, (tmp_path / "server.log").read_text()
                        time.sleep(0.1)
                    response = client.get("/v1/agents?limit=1000")
                    assert response.status_code == 200, response.text
                    agents = response.json()["data"]
                    catalogs.append({a["name"] for a in agents})
                    if settings:
                        by_name = {a["name"]: a for a in agents}
                        assert by_name["custom-pi"]["display_name"] == "Team Assistant"
                        assert by_name["reviewer"]["display_name"] == "Code Reviewer"
                        defaults = [a for a in agents if a.get("default_for_new_sessions")]
                        assert [a["name"] for a in defaults] == ["custom-pi"]
                        assert by_name["custom-pi"]["id"] != by_name["reviewer"]["id"]
                        # Exercise both the advertised default and an explicit profile choice.
                        for selected in (defaults[0], by_name["reviewer"]):
                            created = client.post(
                                "/v1/sessions", json={"agent_id": selected["id"]}
                            )
                            assert created.status_code in (200, 201), created.text
                            session_id = created.json()["id"]
                            session = client.get(f"/v1/sessions/{session_id}")
                            assert session.status_code == 200, session.text
                            assert session.json()["agent_id"] == selected["id"]
            finally:
                proc.terminate()
                proc.wait(timeout=10)
    assert "custom-pi" in catalogs[0]
    assert len(catalogs[0]) > 1
    assert catalogs[1] == {"custom-pi", "reviewer"}
