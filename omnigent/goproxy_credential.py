"""Loopback relay that hands a managed sandbox its owner's Go module proxy token.

The host token stays in host memory. Processes in the sandbox read
``~/.omnigent/goproxy/host.json`` for ``relay_url`` and call it with
``X-Omnigent-Goproxy: 1`` and ``?url=<proxy url>``; the reply is the server's
``goproxy`` host credential (``token``, ``url``, ``prefix``).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urlsplit

import httpx

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

RELAY_HEADER = "X-Omnigent-Goproxy"


class GoProxyAccessError(Exception):
    """A sanitized credential error safe to show to a session owner."""


def credential(server: str, host_id: str, url: str, *, host_token: str) -> dict[str, str]:
    """Fetch the owner's Go proxy credential from the server."""
    try:
        response = httpx.get(
            f"{server.rstrip('/')}/v1/hosts/{quote(host_id, safe='')}/credentials/goproxy",
            params={"url": url},
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
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
        return {
            key: payload[key]
            for key in ("token", "url", "prefix")
            if isinstance(payload.get(key), str)
        }
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        raise GoProxyAccessError(
            "Go proxy access unavailable; renew your Omnigent sign-in or check the proxy URL"
        ) from None


class GoProxyRelay(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, server: str, host_id: str, token: str):
        self.server_url, self.host_id, self.host_token = server, host_id, token
        super().__init__(("127.0.0.1", 0), GoProxyHandler)


class GoProxyHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_GET(self) -> None:
        payload: dict[str, object]
        try:
            parts = urlsplit(self.path)
            query = parse_qs(parts.query, max_num_fields=2)
            if (
                parts.path != "/credential"
                or self.headers.get(RELAY_HEADER) != "1"
                or set(query) != {"url"}
                or len(query["url"]) != 1
            ):
                raise GoProxyAccessError("Invalid Go proxy credential request")
            relay = cast(GoProxyRelay, self.server)
            payload = {
                **credential(
                    relay.server_url, relay.host_id, query["url"][0], host_token=relay.host_token
                ),
                "connected": True,
            }
            status = 200
        except (GoProxyAccessError, ValueError):
            payload = {"error": "Go proxy identity unavailable"}
            status = 403
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def config_path() -> Path:
    return Path.home() / ".omnigent" / "goproxy" / "host.json"


def configure(server: str, host_id: str) -> None:
    """Start the relay and publish its address; a no-op outside a managed POSIX sandbox."""
    if os.environ.get("IS_SANDBOX") != "1" or os.name == "nt":
        return
    token = os.environ.get(HOST_TOKEN_ENV_VAR)
    if not token:
        return
    relay = GoProxyRelay(server, host_id, token)
    threading.Thread(target=relay.serve_forever, daemon=True, name="goproxy-credentials").start()
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump({"relay_url": f"http://127.0.0.1:{relay.server_port}/credential"}, stream)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)
