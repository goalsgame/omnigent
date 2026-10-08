"""Exercise unconnected checkout through the generated shell and warm bootstrap."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from omnigent.host import warm_bootstrap as bootstrap
from omnigent.onboarding.sandboxes.kubernetes import _render_workspace_prep_command
from omnigent.onboarding.sandboxes.types import RepoWorkspace


@pytest.mark.parametrize("available", [True, False])
def test_unconnected_checkout_prepares_public_repo_or_returns_safe_hint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    available: bool,
) -> None:
    source = tmp_path / "source"
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "hello.txt").write_text("hello")
    subprocess.run(["git", "-C", str(source), "add", "hello.txt"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(source),
            "-c",
            "user.name=Example",
            "-c",
            "user.email=example@example.com",
            "commit",
            "-qm",
            "initial",
        ],
        check=True,
    )
    monkeypatch.setenv("OMNIGENT_POD_UID", "test-pod")
    monkeypatch.setenv("OMNIGENT_ACTIVATION_DIR", str(tmp_path / "activation"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    workspace = tmp_path / "workspace"
    command = _render_workspace_prep_command(
        str(workspace),
        [RepoWorkspace(url="https://github.com/example/repo.git", branch=None, repo_name="repo")],
        "https://server.example",
        "test-host",
    )
    # Simulate a definitive unconnected broker; the clone itself uses real Git.
    destination = source if available else tmp_path / "missing"
    rewrite = shlex.quote(f"url.{destination}.insteadOf=https://github.com/example/repo.git")
    script = (
        'python3() { if [ "$1" = "-c" ]; then return 10; fi; command python3 "$@"; }\n'
        f'git() {{ command git -c {rewrite} "$@"; }}\n' + command[2]
    )
    payload = {
        "version": 1,
        "pod_uid": "test-pod",
        "host_id": "test-host",
        "host_name": "checkout-test",
        "token": "credential-sentinel",
        "server_url": "https://server.example",
        "generation": "assignment-1",
        "prepare_command": ["bash", "-c", script],
    }
    bootstrap.activate(payload)
    bootstrap._prepare_once(
        tmp_path / "activation/private",
        bootstrap.Activation.parse(payload),
        bootstrap._Signals(),
    )
    status = bootstrap.status()
    if available:
        assert status["stage"] == "prepared"
        assert "error_code" not in status
        assert (workspace / "repo/hello.txt").read_text() == "hello"
    else:
        assert status["stage"] == "failed"
        assert status["error_code"] == "github_checkout_unconnected"
    assert "credential-sentinel" not in json.dumps(status)
