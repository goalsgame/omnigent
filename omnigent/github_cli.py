"""Per-invocation GitHub CLI authentication in managed host images.

The image's immutable gh shim invokes this module before the real executable.
Only the host-bound broker coordinates are persisted; GitHub access tokens
travel in the child environment and are never placed in command arguments.
"""

from __future__ import annotations

import contextlib
import json
import os
import sys
import tempfile
from pathlib import Path

_GH_EXECUTABLE = "/usr/local/libexec/omnigent/gh"


def broker_path() -> Path:
    """Shared home survives host/runner environment filtering and shell startup."""
    return Path.home() / ".omnigent" / "github" / "gh-broker.json"


def configure_broker(server_url: str, host_id: str, host_token: str) -> bool:
    """Persist only host-scoped authority, atomically and owner-readable only."""
    if os.environ.get("IS_SANDBOX", "").strip() != "1":
        return False
    path = broker_path()
    tmp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".gh-broker-")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump({"server": server_url, "host_id": host_id, "host_token": host_token}, handle)
        os.replace(tmp, path)
    except OSError:
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
    return True


def main(argv: list[str] | None = None) -> int:
    """Fetch current authority then exec gh once; never retry side effects."""
    args = sys.argv[1:] if argv is None else argv
    env = dict(os.environ)
    try:
        if env.get("IS_SANDBOX", "").strip() == "1":
            try:
                raw = broker_path().read_text(encoding="utf-8")
            except FileNotFoundError:
                raw = None
            if raw is not None:
                from omnigent.git_credential_github import _fetch_credential

                config = json.loads(raw)
                if not isinstance(config, dict) or not all(
                    isinstance(config.get(key), str) and config[key]
                    for key in ("server", "host_id", "host_token")
                ):
                    raise ValueError("Invalid broker configuration")
                credential = _fetch_credential(
                    config["server"],
                    config["host_id"],
                    config["host_token"],
                )
                if credential is None:
                    raise ValueError("GitHub credential unavailable")
                token = credential[1]
                if not token.isascii() or any(c.isspace() for c in token):
                    raise ValueError("Invalid broker credential")
                env["GH_TOKEN"] = token
                env.pop("GITHUB_TOKEN", None)
        os.execve(_GH_EXECUTABLE, [_GH_EXECUTABLE, *args], env)
    except (OSError, ValueError, TypeError):
        # Exception messages can contain tokens, URLs or response bodies.
        print(
            "Omnigent: GitHub CLI credential setup failed. Check the managed host's "
            "GitHub connection/broker and retry; no GitHub command was run.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
