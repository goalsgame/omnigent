"""Workspace preservation and per-command credential refresh for Lore."""

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest

from omnigent import lore_checkout as lore
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "host-token")
    monkeypatch.setenv("IS_SANDBOX", "1")
    return tmp_path


def test_credential_errors_are_sanitized(home, monkeypatch):
    def fail(*args, **kwargs):
        return httpx.Response(
            403, request=httpx.Request("GET", "https://server.test"), text="SECRET"
        )

    monkeypatch.setattr(httpx, "get", fail)
    with pytest.raises(lore.LoreCheckoutError, match="renew") as exc:
        lore.credential("https://server.test", "host", "lores://lore.test/game")
    assert "SECRET" not in str(exc.value)


def test_local_host_configuration_is_unchanged(home, monkeypatch):
    monkeypatch.delenv("IS_SANDBOX")
    path = os.environ["PATH"]
    lore.configure("https://server.test", "host")
    assert os.environ["PATH"] == path
    assert not (home / ".omnigent").exists()


def test_retained_checkout_never_fetches_or_overwrites(home, monkeypatch):
    monkeypatch.setattr(lore, "_binary", lambda: sys.executable)
    target = home / "workspace/game"
    (target / ".lore").mkdir(parents=True)
    (target / "work.txt").write_text("unsaved")
    manifest = lore._manifest(target)
    lore._write_json(
        manifest, {"path": str(target), "remote": "lores://lore.test/game", "ref": "main@latest"}
    )
    monkeypatch.setattr(
        lore, "credential", lambda *args: pytest.fail("Unexpected credential fetch")
    )
    lore.prepare("https://server.test", "host", "lores://lore.test/game", target, "main")
    assert (target / "work.txt").read_text() == "unsaved"
    with pytest.raises(lore.LoreCheckoutError, match="refusing to overwrite"):
        lore.prepare("https://server.test", "host", "lores://lore.test/other", target, "main")


def test_each_command_renews_owner_credential(home, monkeypatch):
    lore._write_json(
        lore._home() / "host.json",
        {"binary": sys.executable, "server": "https://server.test", "host_id": "host"},
    )
    workspace = home / "workspace"
    workspace.mkdir()
    lore._write_json(
        lore._home() / "checkouts/game.json",
        {"path": str(workspace), "remote": "lores://lore.test/game"},
    )
    monkeypatch.chdir(workspace)
    tokens = iter(["first", "second"])
    monkeypatch.setattr(lore, "relay_credential", lambda *args: {"token": next(tokens)})
    monkeypatch.setattr(lore, "_binary", lambda: sys.executable)
    calls = []
    monkeypatch.setattr(lore.subprocess, "call", lambda args: calls.append(args) or 0)
    assert lore.execute(["status"]) == 0
    assert lore.execute(["sync"]) == 0
    assert calls == [
        [sys.executable, "--identity-token", "first", "status"],
        [sys.executable, "--identity-token", "second", "sync"],
    ]
    with pytest.raises(lore.LoreCheckoutError, match="different repository"):
        lore.execute(["diff", "lores://other.test/game", "lores://third.test/game"])
    assert "first" not in (lore._home() / "host.json").read_text()


def test_relocated_executable_is_found_without_wrapper_recursion(home, monkeypatch):
    old = home / "removed/lore"
    wrapper = lore._home() / "bin/lore"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text("#!/bin/sh\nexit 99\n")
    wrapper.chmod(0o755)
    replacement = home / "replacement/lore"
    replacement.parent.mkdir()
    replacement.write_text("#!/bin/sh\nexit 0\n")
    replacement.chmod(0o755)
    alias = home / "alias"
    alias.mkdir()
    (alias / "lore").symlink_to(wrapper)
    monkeypatch.setenv(
        "PATH", os.pathsep.join([str(wrapper.parent), str(alias), str(replacement.parent)])
    )
    lore._write_json(lore._home() / "host.json", {"binary": str(old)})
    assert lore._binary() == str(replacement)
    replacement.chmod(0o644)
    with pytest.raises(lore.LoreClientUnavailable):
        lore._binary()


def test_nested_checkouts_choose_their_own_repository(home, monkeypatch):
    parent = home / "workspace"
    child = parent / "child"
    child.mkdir(parents=True)
    lore._write_json(lore._home() / "host.json", {"binary": sys.executable})
    for root, remote in ((parent, "lores://lore.test/parent"), (child, "lores://lore.test/child")):
        lore._write_json(lore._manifest(root), {"path": str(root), "remote": remote})
    calls = []
    monkeypatch.setattr(
        lore, "relay_credential", lambda config, remote: calls.append(remote) or {"token": "child"}
    )
    monkeypatch.setattr(lore.subprocess, "call", lambda args: 0)
    monkeypatch.chdir(child)
    assert lore.execute(["status"]) == 0
    assert calls == ["lores://lore.test/child"]


def test_unsupported_clone_options_fail_before_execution(home, monkeypatch):
    monkeypatch.setattr(lore, "prepare", lambda *args, **kwargs: pytest.fail("Unexpected clone"))
    monkeypatch.setattr(
        lore, "relay_credential", lambda *args: pytest.fail("Unexpected credential fetch")
    )
    with pytest.raises(lore.LoreCheckoutError, match="Managed Lore clone supports"):
        lore._clone(
            ["lores://lore.test/game", "copy", "--view", "custom-view"],
            {},
            "lores://lore.test/game",
        )


@pytest.mark.parametrize("existing_empty", [False, True])
def test_manifest_write_failure_can_retry_without_published_checkout(
    home, monkeypatch, existing_empty
):
    target = home / "workspace/game"
    if existing_empty:
        target.mkdir(parents=True)
    manifest = lore._manifest(target)
    original_write = lore._write_json
    fail = True
    clones = []

    def write(path, value):
        if fail:
            raise OSError("disk full")
        return original_write(path, value)

    def clone(args, **kwargs):
        destination = Path(args[-1])
        (destination / ".lore").mkdir(parents=True)
        (destination / "file.txt").write_text("cloned")
        clones.append(destination)
        return lore.subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(lore, "_write_json", write)
    monkeypatch.setattr(lore, "_binary", lambda: sys.executable)
    monkeypatch.setattr(lore.subprocess, "run", clone)
    with pytest.raises(OSError, match="disk full"):
        lore.prepare(
            "https://server.test",
            "host",
            "lores://lore.test/game",
            target,
            "main",
            credential_payload={"token": "ephemeral"},
        )
    assert not (target / ".lore").exists()
    assert not manifest.exists()
    assert not list(target.parent.glob(".omnigent-lore-*"))
    if existing_empty:
        assert target.is_dir() and not any(target.iterdir())
    fail = False
    lore.prepare(
        "https://server.test",
        "host",
        "lores://lore.test/game",
        target,
        "main",
        credential_payload={"token": "ephemeral"},
    )
    assert (target / "file.txt").read_text() == "cloned"
    assert manifest.exists()
    assert len(clones) == 2


def test_missing_client_wake_preserves_retained_edits(home, monkeypatch):
    target = home / "workspace/game"
    (target / ".lore").mkdir(parents=True)
    (target / "work.txt").write_text("unsaved")
    lore._write_json(
        lore._manifest(target),
        {"path": str(target), "remote": "lores://lore.test/game", "ref": "main@latest"},
    )
    monkeypatch.setenv("PATH", str(home / "missing-bin"))
    monkeypatch.setattr(lore, "credential", lambda *args: pytest.fail("Unexpected fetch"))
    with pytest.raises(lore.LoreClientUnavailable) as exc:
        lore.prepare("https://server.test", "host", "lores://lore.test/game", target, "main")
    assert exc.value.exit_code == 89
    assert (target / "work.txt").read_text() == "unsaved"
    assert (target / ".lore").is_dir()


@pytest.mark.parametrize("same_remote", [False, True])
def test_competing_clones_keep_published_checkout_registration(home, monkeypatch, same_remote):
    target = home / "workspace/game"
    first_remote = "lores://lore.test/first"
    second_remote = first_remote if same_remote else "lores://lore.test/second"
    cloning = threading.Event()
    competing = threading.Event()
    release = threading.Event()
    original_lock = lore._checkout_lock
    clones = []

    @contextmanager
    def lock(manifest):
        if cloning.is_set():
            competing.set()
        with original_lock(manifest):
            yield

    def clone(args, **kwargs):
        clones.append(args[-2])
        cloning.set()
        assert release.wait(10)
        destination = Path(args[-1])
        (destination / ".lore").mkdir(parents=True)
        (destination / "file.txt").write_text(args[-2])
        return lore.subprocess.CompletedProcess(args, 0)

    def prepare(remote):
        lore.prepare(
            "https://server.test",
            "host",
            remote,
            target,
            "main",
            credential_payload={"token": "ephemeral"},
        )

    monkeypatch.setattr(lore, "_checkout_lock", lock)
    monkeypatch.setattr(lore, "_binary", lambda: sys.executable)
    monkeypatch.setattr(lore.subprocess, "run", clone)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(prepare, first_remote)
        try:
            assert cloning.wait(10)
            second = pool.submit(prepare, second_remote)
            assert competing.wait(10)
        finally:
            release.set()
        first.result(timeout=10)
        if same_remote:
            second.result(timeout=10)
        else:
            with pytest.raises(lore.LoreWorkspaceConflict):
                second.result(timeout=10)
    assert clones == [first_remote]
    assert (target / "file.txt").read_text() == first_remote
    assert first_remote in lore._manifest(target).read_text()
    prepare(first_remote)
    if not same_remote:
        with pytest.raises(lore.LoreWorkspaceConflict):
            prepare(second_remote)
