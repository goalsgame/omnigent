"""Managed gh commands must not depend on a periodically materialized token."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

import pytest

from omnigent import git_credential_github as credentials
from omnigent import github_cli
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture
def managed_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "test-host-authority")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    return tmp_path


def test_fresh_broker_token_wins_after_another_workspace_rotates_token(
    managed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current = {"connected": True, "token": "old-test-token", "login": "owner"}
    fetch = Mock(side_effect=lambda *_: dict(current))
    monkeypatch.setattr(credentials, "_fetch", fetch)
    assert credentials.configure_host_gh("http://broker.example", "host-id")
    hosts = Path(os.environ["GH_CONFIG_DIR"]) / "hosts.yml"
    old_hosts = hosts.read_text()
    current["token"] = "new-test-token"
    monkeypatch.setenv("GH_TOKEN", "stale-environment-token")
    monkeypatch.setenv("GITHUB_TOKEN", "shared-environment-token")
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR)
    execute = Mock()
    monkeypatch.setattr(github_cli.os, "execve", execute)

    assert github_cli.main(["pr", "create", "--title", "title with spaces"]) == 0
    executable, argv, env = execute.call_args.args
    assert argv == [executable, "pr", "create", "--title", "title with spaces"]
    assert env["GH_TOKEN"] == "new-test-token"
    assert "GITHUB_TOKEN" not in env
    fetch.assert_called_with("http://broker.example", "host-id", "test-host-authority")
    assert hosts.read_text() == old_hosts  # Timer has not ticked; command still succeeds.
    assert "new-test-token" not in " ".join(argv)
    assert "old-test-token" not in github_cli.broker_path().read_text()
    assert github_cli.broker_path().stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("response", [None, {"connected": False}, {"connected": True}])
def test_broker_failure_never_falls_back_to_saved_or_ambient_tokens(
    managed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys,
    response,
) -> None:
    assert github_cli.configure_broker("http://broker.example", "host-id", "host-authority")
    monkeypatch.setattr(credentials, "_fetch", lambda *_: response)
    monkeypatch.setenv("GH_TOKEN", "ambient-token")
    execute = Mock()
    monkeypatch.setattr(github_cli.os, "execve", execute)
    assert github_cli.main(["api", "user"]) == 1
    execute.assert_not_called()
    output = capsys.readouterr()
    assert "credential" in output.err.lower()
    assert "ambient-token" not in output.err
    assert "host-authority" not in output.err
    assert not output.out


@pytest.mark.parametrize("sandbox", ["0", "1"])
def test_without_broker_configuration_authentication_is_unchanged(
    managed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    sandbox: str,
) -> None:
    monkeypatch.setenv("IS_SANDBOX", sandbox)
    monkeypatch.setenv("GH_TOKEN", "personal-token")
    execute = Mock()
    monkeypatch.setattr(github_cli.os, "execve", execute)
    assert github_cli.main(["--version"]) == 0
    assert execute.call_args.args[2]["GH_TOKEN"] == "personal-token"


def test_personal_host_ignores_existing_managed_configuration(
    managed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert github_cli.configure_broker("http://broker.example", "host-id", "host-authority")
    monkeypatch.delenv("IS_SANDBOX")
    fetch = Mock()
    monkeypatch.setattr(credentials, "_fetch", fetch)
    execute = Mock()
    monkeypatch.setattr(github_cli.os, "execve", execute)
    assert github_cli.main(["--version"]) == 0
    fetch.assert_not_called()


def test_corrupt_configuration_fails_closed(managed_home: Path, monkeypatch, capsys) -> None:
    assert github_cli.configure_broker("http://broker.example", "host-id", "host-authority")
    github_cli.broker_path().write_text("{broken")
    execute = Mock()
    monkeypatch.setattr(github_cli.os, "execve", execute)
    assert github_cli.main(["pr", "create"]) == 1
    execute.assert_not_called()
    assert "Traceback" not in capsys.readouterr().err


def test_initial_broker_outage_still_installs_fail_closed_context(
    managed_home: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(credentials, "_fetch", lambda *_: None)
    assert credentials.configure_host_gh("http://broker.example", "host-id") is False
    assert github_cli.broker_path().exists()


def test_retained_workspace_rebinds_even_when_new_owner_is_unlinked(
    managed_home: Path,
    monkeypatch,
) -> None:
    assert github_cli.configure_broker("http://old.example", "old-host", "old-authority")
    monkeypatch.setattr(credentials, "_fetch", lambda *_: {"connected": False})
    assert credentials.configure_host_gh("http://new.example", "new-host") is False
    config = json.loads(github_cli.broker_path().read_text())
    assert config == {
        "server": "http://new.example",
        "host_id": "new-host",
        "host_token": "test-host-authority",
    }
    execute = Mock()
    monkeypatch.setattr(github_cli.os, "execve", execute)
    assert github_cli.main(["api", "user"]) == 1
    execute.assert_not_called()


def test_never_linked_owner_keeps_legacy_authentication(managed_home: Path, monkeypatch) -> None:
    monkeypatch.setattr(credentials, "_fetch", lambda *_: {"connected": False})
    assert credentials.configure_host_gh("http://broker.example", "host-id") is False
    assert not github_cli.broker_path().exists()


def test_packaged_shim_uses_immutable_executable_and_preserves_arguments() -> None:
    root = Path(__file__).resolve().parents[1]
    shim = (root / "deploy/docker/gh").read_text()
    assert 'exec /opt/venv/bin/python -m omnigent.github_cli "$@"' in shim
    for name in ("Dockerfile", "Dockerfile.ubi"):
        dockerfile = (root / "deploy/docker" / name).read_text()
        assert "mv /usr/local/bin/gh /usr/local/libexec/omnigent/gh" in dockerfile
        assert "COPY --chmod=0755 deploy/docker/gh /usr/local/bin/gh" in dockerfile
    assert github_cli._GH_EXECUTABLE == "/usr/local/libexec/omnigent/gh"


def test_local_host_cannot_write_broker_configuration(managed_home: Path, monkeypatch) -> None:
    monkeypatch.delenv("IS_SANDBOX")
    assert not github_cli.configure_broker("http://broker.example", "host-id", "host-authority")
    assert not github_cli.broker_path().exists()


def test_real_subprocess_renews_each_invocation_and_never_replays_mutation(
    managed_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"token": "initial-test-token", "available": True, "fetches": 0}

    class Broker(BaseHTTPRequestHandler):
        def do_GET(self):
            assert self.path == "/v1/hosts/host-id/credentials/github"
            assert self.headers["X-Omnigent-Host-Token"] == "test-host-authority"
            state["fetches"] += 1
            self.send_response(200 if state["available"] else 503)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"connected": True, "token": state["token"]}).encode())

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    real_gh = managed_home / "real-gh"
    expected = managed_home / "expected-token"
    calls = managed_home / "invocations"
    real_gh.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "from pathlib import Path\n"
        f"assert os.environ['GH_TOKEN'] == Path({str(expected)!r}).read_text()\n"
        f"with Path({str(calls)!r}).open('a') as f: f.write('invoked\\n')\n"
        "assert sys.argv[1:] == ['pr', 'create']\n"
        "print('authenticated')\n"
        "sys.exit(7)\n"
    )
    real_gh.chmod(0o700)
    command = [
        sys.executable,
        "-c",
        "import sys; from omnigent import github_cli; "
        "github_cli._GH_EXECUTABLE=sys.argv[1]; "
        "sys.exit(github_cli.main(sys.argv[2:]))",
        str(real_gh),
        "pr",
        "create",
    ]
    try:
        assert github_cli.configure_broker(
            f"http://127.0.0.1:{server.server_port}",
            "host-id",
            "test-host-authority",
        )
        for token in ("initial-test-token", "rotated-test-token"):
            state["token"] = token
            expected.write_text(token)
            result = subprocess.run(command, text=True, capture_output=True, timeout=20)
            assert result.returncode == 7  # Preserve status; never replay a failed mutation.
            assert result.stdout.strip() == "authenticated"
            assert token not in result.stderr
        assert calls.read_text().splitlines() == ["invoked", "invoked"]
        assert state["fetches"] == 2
        state["available"] = False
        result = subprocess.run(command, text=True, capture_output=True, timeout=20)
        assert result.returncode == 1
        assert calls.read_text().splitlines() == ["invoked", "invoked"]
        assert not result.stdout
        assert state["token"] not in result.stderr
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
