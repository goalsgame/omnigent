"""Fetch a fresh inference bearer using the managed host's launch identity."""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

import httpx

from omnigent.host.identity_env import HOST_TOKEN_ENV_VAR


def _coords_path() -> Path:
    return Path.home() / ".omnigent-inference-broker.json"


def configure_host_inference(server_url: str, host_id: str) -> None:
    """Record launch credentials privately; never persist the inference bearer."""
    token = os.environ.get(HOST_TOKEN_ENV_VAR)
    if os.environ.get("IS_SANDBOX") != "1" or not token:
        return
    path = _coords_path()
    try:
        fd, temporary = tempfile.mkstemp(prefix=".inference-broker-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump({"server": server_url, "host_id": host_id, "host_token": token}, stream)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
    except OSError:
        logging.getLogger(__name__).warning("Could not configure host inference credentials")


def main() -> int:
    """Print only a bearer on success; fail closed with sanitized stderr otherwise."""
    try:
        coords = json.loads(_coords_path().read_text())
        url = (
            f"{coords['server'].rstrip('/')}/v1/hosts/"
            f"{quote(coords['host_id'], safe='')}/credentials/openrouter"
        )
        response = httpx.get(
            url,
            headers={"X-Omnigent-Host-Token": coords["host_token"]},
            timeout=8.0,
            follow_redirects=False,
            trust_env=False,
        )
        response.raise_for_status()
        result = response.json()
        token = result.get("token")
        if (
            result.get("connected") is not True
            or not isinstance(token, str)
            or not token
            or any(c.isspace() for c in token)
        ):
            raise ValueError("credential unavailable")
        sys.stdout.write(token + "\n")
        return 0
    except (OSError, ValueError, KeyError, TypeError, AttributeError, httpx.HTTPError):
        print("OpenRouter workload credential unavailable", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
