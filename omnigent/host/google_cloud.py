"""Loopback Google metadata adapter backed by the managed owner's credential broker."""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import cast
from urllib.parse import quote, unquote, urlsplit

import httpx

from omnigent.host.identity_env import HOST_TOKEN_ENV_VAR

_METADATA_PREFIX = "/computeMetadata/v1/"


class GoogleCloudNotConnected(ValueError):
    """The owner has no Google Cloud connection."""


class GoogleCloudAccessRequired(ValueError):
    """The session owner has not granted sandbox access."""


class GoogleCloudMetadataServer(ThreadingHTTPServer):
    """Vend only short-lived owner tokens; never proxy node metadata or ID tokens."""

    daemon_threads = True

    def __init__(self, server_url: str, host_id: str, host_token: str) -> None:
        self.broker_url = (
            f"{server_url.rstrip('/')}/v1/hosts/{quote(host_id, safe='')}/credentials/google_cloud"
        )
        self.host_token = host_token
        super().__init__(("127.0.0.1", 0), GoogleCloudMetadataHandler)

    def credential(self) -> dict[str, object]:
        response = httpx.get(
            self.broker_url,
            headers={"X-Omnigent-Host-Token": self.host_token},
            timeout=10,
            trust_env=False,
            follow_redirects=False,
        )
        response.raise_for_status()
        data = response.json()
        if data.get("connected") is False:
            reason = data.get("reason", "")
            if (
                isinstance(reason, str)
                and reason.startswith("session_access_")
                and reason != "session_access_not_connected"
            ):
                raise GoogleCloudAccessRequired(
                    "Approve Google Cloud access in this Omnigent session, then retry"
                )
            raise GoogleCloudNotConnected("Google Cloud connection unavailable")
        token, email, expiry = data.get("token"), data.get("email"), data.get("expires_at")
        if (
            data.get("connected") is not True
            or not isinstance(token, str)
            or not token
            or any(c.isspace() for c in token)
            or not isinstance(email, str)
            or not email
            or "/" in email
            or "\n" in email
            or not isinstance(expiry, (int, float))
            or expiry <= time.time() + 5
        ):
            raise ValueError("Google Cloud connection unavailable")
        return {"token": token, "email": email, "expires_in": int(expiry - time.time())}


class GoogleCloudMetadataHandler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        """Do not log credential requests or responses."""

    def reply(self, status: int, data: object, *, structured: bool = False) -> None:
        body = (json.dumps(data) if structured else str(data)).encode()
        self.send_response(status)
        self.send_header("Metadata-Flavor", "Google")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Type", "application/json" if structured else "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if self.headers.get("Metadata-Flavor") != "Google":
            self.reply(403, "Metadata-Flavor: Google is required")
            return
        path = unquote(urlsplit(self.path).path)
        if path == "/":
            self.reply(200, "")
            return
        if not path.startswith(_METADATA_PREFIX):
            self.reply(404, "Unknown metadata path")
            return
        path = path.removeprefix(_METADATA_PREFIX).rstrip("/")
        if path in (
            "project/project-id",
            "project/numeric-project-id",
            "universe/universe-domain",
        ):
            # No resource project is selected on the user's behalf. Zero is the
            # SDK's numeric residency probe, not a real project number.
            values = {
                "project/project-id": "",
                "project/numeric-project-id": "0",
                "universe/universe-domain": "googleapis.com",
            }
            self.reply(200, values[path])
            return
        if not path.startswith("instance/service-accounts") or path.endswith("/identity"):
            self.reply(404, "Unsupported metadata path")
            return
        try:
            credential = cast(GoogleCloudMetadataServer, self.server).credential()
        except GoogleCloudAccessRequired:
            self.reply(
                403,
                "Google Cloud access requires the session owner's approval in Omnigent; "
                "approve, then retry",
            )
            return
        except GoogleCloudNotConnected:
            self.reply(403, "Connect Google Cloud in Omnigent Sandbox Integrations, then retry")
            return
        except (httpx.HTTPError, ValueError, TypeError, KeyError, AttributeError):
            self.reply(503, "Google Cloud credentials are temporarily unavailable; retry shortly")
            return
        email = str(credential["email"])
        prefix = "instance/service-accounts"
        if path == prefix:
            self.reply(200, f"default/\n{email}/\n")
            return
        account, _, field = path.removeprefix(prefix + "/").partition("/")
        if account not in ("default", email):
            self.reply(404, "Unknown account")
        elif field == "token":
            self.reply(
                200,
                {
                    "access_token": credential["token"],
                    "token_type": "Bearer",
                    "expires_in": credential["expires_in"],
                },
                structured=True,
            )
        elif field == "email":
            self.reply(200, email)
        elif field == "scopes":
            self.reply(200, "https://www.googleapis.com/auth/cloud-platform\n")
        elif not field:
            self.reply(
                200,
                {
                    "email": email,
                    "aliases": ["default"],
                    "scopes": ["https://www.googleapis.com/auth/cloud-platform"],
                },
                structured=True,
            )
        else:
            self.reply(404, "Unsupported account metadata")


def start_host_google_cloud(server_url: str, host_id: str) -> GoogleCloudMetadataServer | None:
    """Start only for opt-in managed hosts, with refresh owned by the server."""
    token = os.environ.get(HOST_TOKEN_ENV_VAR)
    if (
        os.environ.get("IS_SANDBOX") != "1"
        or os.environ.get("OMNIGENT_GOOGLE_CLOUD_AUTH") != "1"
        or not token
    ):
        return None
    server = GoogleCloudMetadataServer(server_url, host_id, token)
    address = f"127.0.0.1:{server.server_port}"
    for name in ("GCE_METADATA_HOST", "GCE_METADATA_ROOT", "GCE_METADATA_IP"):
        os.environ[name] = address
    # Separate gcloud's cache from personal CLI credentials copied into a home.
    os.environ["CLOUDSDK_CONFIG"] = os.path.expanduser("~/.omnigent/gcloud")
    os.environ.pop("CLOUDSDK_ACTIVE_CONFIG_NAME", None)
    threading.Thread(
        target=server.serve_forever, daemon=True, name="google-cloud-metadata"
    ).start()
    return server
