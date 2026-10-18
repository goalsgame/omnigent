"""Managed Lore checkout and per-command owner credential renewal."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, urlsplit

import httpx

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER
from omnigent.host.workspace_errors import (
    LORE_ACCESS_UNAVAILABLE_EXIT,
    LORE_CHECKOUT_FAILED_EXIT,
    LORE_CLIENT_UNAVAILABLE_EXIT,
    LORE_WORKSPACE_CONFLICT_EXIT,
)


class LoreCheckoutError(Exception):
    """A sanitized checkout or identity error safe to show to a session owner."""

    exit_code = LORE_CHECKOUT_FAILED_EXIT


class LoreAccessError(LoreCheckoutError):
    exit_code = LORE_ACCESS_UNAVAILABLE_EXIT


class LoreWorkspaceConflict(LoreCheckoutError):
    exit_code = LORE_WORKSPACE_CONFLICT_EXIT


class LoreClientUnavailable(LoreCheckoutError):
    exit_code = LORE_CLIENT_UNAVAILABLE_EXIT


def credential(server: str, host_id: str, remote: str, *, host_token: str | None = None) -> dict:
    token = host_token or os.environ.get(HOST_TOKEN_ENV_VAR, "")
    if not token:
        raise LoreAccessError("Lore checkout requires a managed host credential")
    try:
        response = httpx.get(
            server.rstrip("/") + f"/v1/hosts/{host_id}/credentials/lore",
            params={"remote": remote},
            headers={MANAGED_HOST_TOKEN_HEADER: token},
            timeout=20,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("connected") is not True
            or not isinstance(payload.get("token"), str)
            or not payload["token"]
            or payload["token"].startswith("-")
            or any(c.isspace() for c in payload["token"])
        ):
            raise ValueError("Missing credential")
        return payload
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise LoreAccessError(
            "Lore access unavailable: verify the repository and renew your "
            "Omnigent sign-in or machine delegation"
        ) from None


class LoreCredentialRelay(ThreadingHTTPServer):
    """Keep the host capability in host memory, outside the runner environment."""

    daemon_threads = True

    def __init__(self, server: str, host_id: str, token: str):
        self.server_url, self.host_id, self.host_token = server, host_id, token
        super().__init__(("127.0.0.1", 0), LoreCredentialHandler)


class LoreCredentialHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        try:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query, max_num_fields=2)
            if (
                parts.path != "/credential"
                or self.headers.get("X-Omnigent-Lore") != "1"
                or set(query) != {"remote"}
                or len(query["remote"]) != 1
            ):
                raise LoreCheckoutError("Invalid Lore credential request")
            server = cast(LoreCredentialRelay, self.server)
            payload = credential(
                server.server_url, server.host_id, query["remote"][0], host_token=server.host_token
            )
            status = 200
        except (LoreCheckoutError, ValueError):
            payload, status = (
                {"error": "Lore identity unavailable; renew delegation or check policy"},
                403,
            )
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def relay_credential(config: dict, remote: str) -> dict:
    url = config.get("relay_url", "")
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname != "127.0.0.1"
        or parts.path != "/credential"
        or parts.username
        or parts.password
        or parts.query
        or parts.fragment
    ):
        raise LoreAccessError("Managed Lore credential relay is unavailable")
    try:
        response = httpx.get(
            url,
            params={"remote": remote},
            headers={"X-Omnigent-Lore": "1"},
            timeout=25,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        payload = response.json()
        if (
            not isinstance(payload, dict)
            or payload.get("connected") is not True
            or not isinstance(payload.get("token"), str)
            or not payload["token"]
            or payload["token"].startswith("-")
            or any(c.isspace() for c in payload["token"])
        ):
            raise ValueError("Missing credential")
        return payload
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise LoreAccessError(
            "Lore access unavailable; renew delegation or check repository policy"
        ) from None


def _home() -> Path:
    return Path.home() / ".omnigent" / "lore"


def _write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def configure(server: str, host_id: str, *, relay: bool = True) -> None:
    """Install the managed wrapper only in disposable POSIX sandboxes."""
    if os.environ.get("IS_SANDBOX") != "1" or os.name == "nt":
        return
    root = _home()
    if relay and not any((root / "checkouts").glob("*.json")):
        return
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    config_path = root / "host.json"
    previous = json.loads(config_path.read_text()) if config_path.exists() else {}
    binary = _find_binary(previous.get("binary"))
    if not binary:
        return
    config = {"server": server, "host_id": host_id, "binary": binary}
    if relay:
        token = os.environ.get(HOST_TOKEN_ENV_VAR)
        if not token:
            return
        local = LoreCredentialRelay(server, host_id, token)
        config["relay_url"] = f"http://127.0.0.1:{local.server_port}/credential"
        threading.Thread(target=local.serve_forever, daemon=True, name="lore-credentials").start()
    _write_json(config_path, config)
    wrapper_dir = root / "bin"
    wrapper_dir.mkdir(exist_ok=True, mode=0o700)
    wrapper = wrapper_dir / "lore"
    wrapper.write_text(
        "#!/bin/sh\nexec "
        + shlex.quote(sys.executable)
        + ' -I -m omnigent.lore_checkout exec "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o700)
    paths = os.environ.get("PATH", "").split(os.pathsep)
    os.environ["PATH"] = os.pathsep.join(
        [str(wrapper_dir), *[p for p in paths if p != str(wrapper_dir)]]
    )


def _find_binary(cached: object = None) -> str | None:
    wrapper = (_home() / "bin/lore").resolve()
    candidates = [cached] + [
        shutil.which("lore", path=directory)
        for directory in os.environ.get("PATH", os.defpath).split(os.pathsep)
        if directory and Path(directory).resolve() != wrapper.parent
    ]
    for candidate in candidates:
        if not isinstance(candidate, str) or not Path(candidate).is_absolute():
            continue
        try:
            resolved = Path(candidate).resolve()
            if resolved != wrapper and resolved.is_file() and os.access(resolved, os.X_OK):
                return str(resolved)
        except (OSError, RuntimeError):
            continue
    return None


def _binary() -> str:
    path = _home() / "host.json"
    cached = json.loads(path.read_text()).get("binary") if path.exists() else None
    binary = _find_binary(cached)
    if not binary:
        raise LoreClientUnavailable("Lore CLI is not installed in this runner image")
    return binary


def _manifest(target: Path) -> Path:
    key = hashlib.sha256(str(target.resolve()).encode()).hexdigest()
    return _home() / "checkouts" / (key + ".json")


@contextmanager
def _checkout_lock(manifest: Path) -> Iterator[None]:
    # Lock files remain in place so waiting processes always share the same inode.
    import fcntl

    manifest.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(manifest.with_suffix(".lock"), os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def prepare(
    server: str,
    host_id: str,
    remote: str,
    target: Path,
    ref: str | None,
    *,
    credential_payload: dict | None = None,
    exact_revision: bool = False,
) -> None:
    """Clone into owned staging storage; retained checkouts are never synchronized."""
    from omnigent.lore_urls import lore_remote

    lore_remote(remote)
    target = target.absolute()
    if target.is_symlink() or (target.exists() and not target.is_dir()):
        raise LoreWorkspaceConflict("Refusing to replace a symlink or non-directory workspace")
    target = target.resolve()
    manifest = _manifest(target)
    if ref and not exact_revision and "@" not in ref and not re.fullmatch(r"[0-9a-fA-F]{64}", ref):
        ref += "@latest"
    with _checkout_lock(manifest):
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            raise LoreWorkspaceConflict("Refusing to replace a symlink or non-directory workspace")
        if target.exists() and any(target.iterdir()):
            stored = json.loads(manifest.read_text()) if manifest.exists() else {}
            if (
                stored != {"path": str(target), "remote": remote, "ref": ref}
                or not (target / ".lore").is_dir()
            ):
                raise LoreWorkspaceConflict(
                    "Workspace is not the retained Lore checkout; refusing to overwrite it"
                )
            _binary()
            return
        binary = _binary()
        payload = (
            credential_payload
            if credential_payload is not None
            else credential(server, host_id, remote)
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".omnigent-lore-", dir=target.parent) as temporary:
            staging = Path(temporary)
            args = [binary, "--identity-token", payload["token"], "clone"]
            if ref:
                args += ["--revision", ref]
            if payload.get("view"):
                view = staging / "view.txt"
                view.write_text(payload["view"], encoding="utf-8")
                args += ["--view", str(view)]
            args += [remote, str(staging / "clone")]
            result = subprocess.run(args, capture_output=True, text=True)
            if result.returncode:
                raise LoreCheckoutError(
                    "Lore clone failed; verify access, revision, view and available disk space"
                )
            if not (staging / "clone" / ".lore").is_dir():
                raise LoreCheckoutError("Lore clone did not create repository metadata")
            _write_json(manifest, {"path": str(target), "remote": remote, "ref": ref})
            if target.exists():
                target.rmdir()
            (staging / "clone").rename(target)


def _clone(args: list[str], config: dict, remote: str) -> int:
    parser = argparse.ArgumentParser(prog="lore clone", add_help=False, exit_on_error=False)
    parser.add_argument("remote")
    parser.add_argument("target", nargs="?")
    refs = parser.add_mutually_exclusive_group()
    refs.add_argument("--branch")
    refs.add_argument("--revision")
    try:
        options, unknown = parser.parse_known_intermixed_args(args)
    except argparse.ArgumentError:
        raise LoreCheckoutError("Invalid managed Lore clone arguments") from None
    if unknown or options.remote != remote:
        raise LoreCheckoutError(
            "Managed Lore clone supports URL, target, --branch and --revision; "
            "the view is configured by the operator"
        )
    target = Path(options.target or urlsplit(remote).path.rsplit("/", 1)[-1])
    payload = relay_credential(config, remote)
    prepare(
        config["server"],
        config["host_id"],
        remote,
        target,
        options.revision or options.branch,
        credential_payload=payload,
        exact_revision=options.revision is not None,
    )
    return 0


def execute(args: list[str]) -> int:
    config = json.loads((_home() / "host.json").read_text())
    if any(arg in ("--help", "-h", "--version", "-V") for arg in args):
        return subprocess.call([_binary(), *args])
    if args[:2] == ["repository", "clone"]:
        args = ["clone", *args[2:]]
    remote = next((arg for arg in args if arg.startswith("lores://")), None)
    cwd = Path.cwd().resolve()
    if remote is None:
        candidates = []
        for path in (_home() / "checkouts").glob("*.json"):
            checkout = json.loads(path.read_text())
            if cwd.is_relative_to(Path(checkout["path"])):
                candidates.append(checkout)
        if candidates:
            remote = max(candidates, key=lambda item: len(Path(item["path"]).parts))["remote"]
    command = [_binary()]
    if remote:
        if any(
            arg.split("=", 1)[0] in ("--identity", "--identity-token", "--access-token")
            for arg in args
        ):
            raise LoreCheckoutError("Managed Lore commands use the session owner's identity")
        if any(arg.startswith("lores://") and arg != remote for arg in args):
            raise LoreCheckoutError("Lore command targets a different repository")
        if args and args[0] == "clone" and not any(arg in ("--help", "-h") for arg in args):
            return _clone(args[1:], config, remote)
        if "clone" in args and any(arg.startswith("lores://") for arg in args):
            raise LoreCheckoutError(
                "Managed clone requires 'lore clone' or 'lore repository clone'"
            )
        payload = relay_credential(config, remote)
        command += ["--identity-token", payload["token"]]
    return subprocess.call([*command, *args])


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        if argv and argv[0] == "exec":
            return execute(argv[1:])
        parser = argparse.ArgumentParser()
        parser.add_argument("--server", required=True)
        parser.add_argument("--host-id", required=True)
        parser.add_argument("--remote", required=True)
        parser.add_argument("--target", type=Path, required=True)
        parser.add_argument("--ref")
        args = parser.parse_args(argv)
        configure(args.server, args.host_id, relay=False)
        prepare(args.server, args.host_id, args.remote, args.target, args.ref)
        return 0
    except LoreCheckoutError as exc:
        print(str(exc), file=sys.stderr)
        return exc.exit_code
    except (OSError, ValueError):
        print(
            "Lore workspace preparation failed: "
            "check repository, identity, revision and disk capacity",
            file=sys.stderr,
        )
        return LORE_CHECKOUT_FAILED_EXIT


if __name__ == "__main__":
    raise SystemExit(main())
