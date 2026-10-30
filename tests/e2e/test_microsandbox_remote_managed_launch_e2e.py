"""A managed session on a remote microsandbox host launches a host that registers.

A real ``omnigent server`` is configured with ``provider: microsandbox_remote``
against a stand-in sandbox host that implements the documented host API and runs
each sandbox's commands as local processes in a private home directory. The
server's real launch path provisions the sandbox, runs workspace setup and starts
``omnigent host`` through it; the host connects its tunnel and registers. Deleting
the session removes the sandbox. No LLM turn runs.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.live_server import find_free_port

_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOKEN = "e2e-host-api-token"
_HEALTH_TIMEOUT_S = 180.0
_REGISTER_TIMEOUT_S = 180.0
_POLL_INTERVAL_S = 0.5

pytestmark = pytest.mark.timeout(600)


def _pythonpath() -> str:
    return os.pathsep.join(
        [
            str(_REPO_ROOT),
            str(_REPO_ROOT / "sdks" / "python-client"),
            str(_REPO_ROOT / "sdks" / "ui"),
            os.environ.get("PYTHONPATH", ""),
        ]
    )


def _process_groups(home: Path) -> list[int]:
    record = home / ".sandbox-process-groups"
    return [int(line) for line in record.read_text().split()] if record.exists() else []


def _live_groups(groups: list[int]) -> list[int]:
    live = []
    for group in groups:
        try:
            os.killpg(group, 0)
        except (ProcessLookupError, PermissionError):
            continue
        live.append(group)
    return live


def _kill_groups(groups: list[int]) -> None:
    for group in groups:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(group, signal.SIGKILL)


class FakeSandboxHost(ThreadingHTTPServer):
    """Host API stand-in whose sandboxes are local process trees."""

    def __init__(self, root: Path) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.root = root
        self.tools = root / "bin"
        self.tools.mkdir(parents=True)
        setsid = self.tools / "setsid"
        setsid.write_text(
            f"#!{sys.executable}\n"
            "import os, sys\n"
            "os.setsid()\n"
            "with open(os.path.join(os.environ['HOME'], '.sandbox-process-groups'), 'a') as f:\n"
            "    f.write(f'{os.getpgid(0)}\\n')\n"
            "os.execvp(sys.argv[1], sys.argv[1:])\n"
        )
        setsid.chmod(0o755)
        self.sandboxes: dict[str, dict[str, object]] = {}
        self.calls: list[tuple[str, str, dict[str, object] | None]] = []
        self.lock = threading.Lock()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _Handler(BaseHTTPRequestHandler):
    server: FakeSandboxHost

    def log_message(self, format: str, *args: object) -> None:
        return

    def _reply(self, status: int, body: object | None = None) -> None:
        payload = b"" if body is None else json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _handle(self, method: str) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        with self.server.lock:
            self.server.calls.append((method, self.path, body))
        if self.headers.get("Authorization") != f"Bearer {_TOKEN}":
            self._reply(403, {"detail": "caller not allowed"})
            return
        parts = self.path.strip("/").split("/")
        if parts == ["v1", "host"]:
            self._reply(200, {"cpus": os.cpu_count()})
            return
        name, action = parts[2], (parts[3] if len(parts) > 3 else None)
        sandboxes = self.server.sandboxes
        if method == "PUT":
            if name not in sandboxes:
                home = self.server.root / name / "home"
                home.mkdir(parents=True)
                sandboxes[name] = {"status": "running", "home": home, "env": body["env"]}
            self._reply(200, {"name": name, "created": True, "status": "running"})
            return
        if name not in sandboxes:
            self._reply(204 if method == "DELETE" else 404, None if method == "DELETE" else {})
            return
        box = sandboxes[name]
        if method == "DELETE":
            _kill_groups(_process_groups(box["home"]))  # type: ignore[arg-type]
            shutil.rmtree(self.server.root / name, ignore_errors=True)
            del sandboxes[name]
            self._reply(204)
        elif method == "GET":
            self._reply(200, {"name": name, "status": box["status"]})
        elif action == "touch":
            self._reply(204 if box["status"] == "running" else 409)
        elif action == "stop":
            _kill_groups(_process_groups(box["home"]))  # type: ignore[arg-type]
            box["status"] = "stopped"
            self._reply(200, {"name": name, "status": "stopped"})
        elif action == "start":
            box["status"] = "running"
            self._reply(200, {"name": name, "status": "running"})
        elif action == "exec":
            self._exec(name, box, body)
        else:
            self._reply(400, {})

    def _exec(self, name: str, box: dict[str, object], body: dict[str, object]) -> None:
        if box["status"] != "running":
            self._reply(409, {"detail": "not running"})
            return
        env = {
            **os.environ,
            **box["env"],  # type: ignore[dict-item]
            "HOME": str(box["home"]),
            "PATH": os.pathsep.join(
                [str(self.server.tools), str(Path(sys.executable).parent), os.environ["PATH"]]
            ),
            "PYTHONPATH": _pythonpath(),
        }
        try:
            result = subprocess.run(
                ["sh", "-c", str(body["command"])],
                cwd=str(box["home"]),
                env=env,
                capture_output=True,
                text=True,
                timeout=float(body.get("timeout_s", 600)),  # type: ignore[arg-type]
            )
        except subprocess.TimeoutExpired:
            self._reply(200, {"exit_code": None, "stdout": "", "stderr": "", "timed_out": True})
            return
        self._reply(
            200,
            {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "timed_out": False,
            },
        )

    def do_GET(self) -> None:
        self._handle("GET")

    def do_PUT(self) -> None:
        self._handle("PUT")

    def do_POST(self) -> None:
        self._handle("POST")

    def do_DELETE(self) -> None:
        self._handle("DELETE")


def _spawn_server(
    tmp_path: Path, port: int, host_url: str
) -> tuple[subprocess.Popen[bytes], Path]:
    config_path = tmp_path / "server-config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "sandbox": {
                    "server_url": f"http://127.0.0.1:{port}",
                    "provider": "microsandbox_remote",
                    "microsandbox_remote": {
                        "endpoint": host_url,
                        "token_env": "E2E_HOST_API_TOKEN",
                        "image": "registry.example/omnigent-host:e2e",
                        "cpus": 8,
                        "memory_mib": 2048,
                        "max_memory_mib": 8192,
                        "idle_timeout_s": 3600,
                        "workspace_path": "/home/omnigent",
                        "workspace_quota_mib": 4096,
                    },
                }
            }
        )
    )
    env = {
        **os.environ,
        "PYTHONPATH": _pythonpath(),
        "E2E_HOST_API_TOKEN": _TOKEN,
        "OPENAI_API_KEY": "unused-no-turn-runs",
        "OMNIGENT_BUILTIN_AGENT_DIRS": str(
            _REPO_ROOT / "tests" / "resources" / "agents" / "sdk-chat-builtin.yaml"
        ),
    }
    log_path = tmp_path / "server.log"
    with open(log_path, "w") as log_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
                "server",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{tmp_path / 'e2e.db'}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
                "--config",
                str(config_path),
            ],
            env=env,
            cwd=str(_REPO_ROOT),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
    return proc, log_path


def _wait_for(predicate, timeout_s: float, failure):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(_POLL_INTERVAL_S)
    pytest.fail(failure())


def test_managed_session_launches_a_registered_host_on_the_remote_host(tmp_path: Path) -> None:
    fake = FakeSandboxHost(tmp_path / "sandboxes")
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    port = find_free_port()
    proc, log_path = _spawn_server(tmp_path, port, fake.url)
    base_url = f"http://127.0.0.1:{port}"
    launched_groups: list[int] = []

    def server_log() -> str:
        return log_path.read_text()[-4000:]

    try:

        def healthy() -> bool:
            if proc.poll() is not None:
                pytest.fail(f"server exited early:\n{server_log()}")
            try:
                return httpx.get(f"{base_url}/health", timeout=2.0).status_code == 200
            except httpx.HTTPError:
                return False

        _wait_for(healthy, _HEALTH_TIMEOUT_S, lambda: f"server never healthy:\n{server_log()}")
        assert httpx.get(f"{base_url}/v1/info", timeout=10.0).json()["managed_sandboxes_enabled"]
        agents = httpx.get(f"{base_url}/v1/agents", timeout=10.0).json()["data"]
        [agent] = [a for a in agents if a["name"] == "sdk-chat-builtin"]
        response = httpx.post(
            f"{base_url}/v1/sessions",
            json={"agent_id": agent["id"], "host_type": "managed"},
            timeout=180.0,
        )
        assert response.status_code == 201, response.text[:500]
        session_id = response.json()["id"]

        def registered_host() -> dict[str, object] | None:
            hosts = httpx.get(f"{base_url}/v1/hosts", timeout=10.0).json()["hosts"]
            return next(
                (
                    h
                    for h in hosts
                    if h.get("sandbox_provider") == "microsandbox_remote"
                    and h.get("status") == "online"
                ),
                None,
            )

        host = _wait_for(
            registered_host,
            _REGISTER_TIMEOUT_S,
            lambda: (
                f"managed host never registered online; host API calls "
                f"{[(c[0], c[1]) for c in fake.calls]}:\n{server_log()}"
            ),
        )
        [create] = [c for c in fake.calls if c[0] == "PUT"]
        name = create[1].rsplit("/", 1)[-1]
        assert create[2] == {
            "image": "registry.example/omnigent-host:e2e",
            "cpus": 8,
            "memory_mib": 2048,
            "max_memory_mib": 8192,
            "idle_timeout_s": 3600,
            "env": {},
            "workspace_path": "/home/omnigent",
            "workspace_quota_mib": 4096,
        }
        assert any(c[0] == "POST" and c[1].endswith(f"/{name}/exec") for c in fake.calls)
        groups = _process_groups(fake.sandboxes[name]["home"])  # type: ignore[arg-type]
        launched_groups.extend(groups)
        assert _live_groups(groups), "no host process is running in the sandbox"
        assert host["name"]

        deleted = httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=60.0)
        assert deleted.status_code in (200, 204), deleted.text[:500]
        _wait_for(
            lambda: name not in fake.sandboxes,
            120.0,
            lambda: f"session delete did not remove the sandbox:\n{server_log()}",
        )
        _wait_for(
            lambda: not _live_groups(groups),
            30.0,
            lambda: f"host process groups survived the delete: {_live_groups(groups)}",
        )
    finally:
        _kill_groups(launched_groups)
        for box in list(fake.sandboxes.values()):
            _kill_groups(_process_groups(box["home"]))  # type: ignore[arg-type]
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
        fake.shutdown()
