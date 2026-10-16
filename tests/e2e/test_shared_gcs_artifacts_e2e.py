"""Two server stores share artifacts over the real GCS SDK and HTTP transport.

The local HTTP fixture implements the GCS object operations used here. It does
not emulate IAM; bucket permissions and Workload Identity need deployment checks.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

import google_crc32c
import httpx
import pytest
from google.auth.credentials import AnonymousCredentials
from google.cloud import storage

from omnigent.stores.artifact_store.gcs import GCSArtifactStore
from tests.e2e.conftest import find_free_port
from tests.server.helpers import build_agent_bundle


@pytest.fixture
def gcs_endpoint() -> Iterator[str]:
    objects: dict[str, bytes] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, status: int, payload: bytes = b"", **headers: str) -> None:
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            assert self.path.startswith("/upload/storage/v1/b/test-artifacts/o?")
            content_type = self.headers["Content-Type"]
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = BytesParser().parsebytes(
                f"Content-Type: {content_type}\r\n\r\n".encode() + body
            )
            metadata, content = message.get_payload()
            name = json.loads(metadata.get_payload(decode=True))["name"]
            data = content.get_payload(decode=True)
            objects[name] = data
            crc = base64.b64encode(google_crc32c.value(data).to_bytes(4, "big")).decode()
            self.reply(
                200,
                json.dumps({"name": name, "crc32c": crc}).encode(),
                **{"Content-Type": "application/json"},
            )

        def do_GET(self) -> None:
            path = urlsplit(self.path)
            if "/o/" not in path.path:
                self.reply(404)
                return
            name = unquote(path.path.split("/o/", 1)[1])
            if name not in objects:
                self.reply(404, b'{"error": {"message": "missing"}}')
            elif "alt=media" in path.query:
                crc = base64.b64encode(
                    google_crc32c.value(objects[name]).to_bytes(4, "big")
                ).decode()
                self.reply(200, objects[name], **{"x-goog-hash": f"crc32c={crc}"})
            else:
                self.reply(
                    200,
                    json.dumps({"name": name}).encode(),
                    **{"Content-Type": "application/json"},
                )

        def do_DELETE(self) -> None:
            name = unquote(urlsplit(self.path).path.split("/o/", 1)[1])
            if name in objects:
                del objects[name]
                self.reply(204)
            else:
                self.reply(404, b'{"error": {"message": "missing"}}')

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join(timeout=5)


def test_artifacts_survive_server_store_replacement(
    gcs_endpoint: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from deploy.docker.entrypoint import _ResolvedConfig, _select_artifact_store
    from omnigent.cli import _create_artifact_store

    # Only credentials/endpoint differ from production; the Google SDK performs
    # the uploads, downloads, checksums, metadata lookups and deletions over HTTP.
    real_client = storage.Client
    monkeypatch.setattr(
        storage,
        "Client",
        lambda: real_client(
            project="test-project",
            credentials=AnonymousCredentials(),
            client_options={"api_endpoint": gcs_endpoint},
        ),
    )
    uri = "gs://test-artifacts/server-artifacts"
    key = "agents/example/bundle.tar.gz"
    payload = bytes(range(256)) * 100
    with ExitStack() as clients:
        server_a = _select_artifact_store(
            _ResolvedConfig(
                cfg={},
                database_url="sqlite://",
                artifact_dir=tmp_path / "a",
                artifact_store_uri=uri,
                host="127.0.0.1",
                port=8000,
            )
        )
        server_b = _create_artifact_store(str(tmp_path / "b"), store_uri=uri)
        clients.callback(server_a._client.close)
        clients.callback(server_b._client.close)
        server_a.put(key, payload)
        assert server_b.exists(key)
        assert server_b.get(key) == payload
        server_b.put("uploads/empty.txt", b"")
        assert server_a.get("uploads/empty.txt") == b""
        replacement = GCSArtifactStore(uri)
        clients.callback(replacement._client.close)
        assert replacement.get(key) == payload
        replacement.put(key, b"updated bundle")
        assert server_b.get(key) == b"updated bundle"
        replacement.delete(key)
        assert not server_a.exists(key)
        with pytest.raises(KeyError):
            server_b.get(key)
        server_a.delete(key)
        other_prefix = GCSArtifactStore("gs://test-artifacts/other")
        clients.callback(other_prefix._client.close)
        assert not other_prefix.exists("uploads/empty.txt")


@contextmanager
def _server(tmp_path: Path, name: str, endpoint: str) -> Iterator[httpx.Client]:
    port = find_free_port()
    config = tmp_path / f"{name}.yaml"
    config.write_text(
        json.dumps(
            {
                "artifact_store_uri": "gs://test-artifacts/shared",
                "artifact_location": str(tmp_path / name),
            }
        )
    )
    env = {
        **os.environ,
        "DATABASE_URL": f"sqlite:///{tmp_path / 'shared.db'}",
        "OMNIGENT_CONFIG": str(config),
        "OMNIGENT_DATA_DIR": str(tmp_path / f"data-{name}"),
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_AUTH_ENABLED": "0",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
        "STORAGE_EMULATOR_HOST": endpoint,
        "GOOGLE_CLOUD_PROJECT": "test-project",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = str(root)
    with (tmp_path / f"{name}.log").open("wb") as log:
        proc = subprocess.Popen(
            [sys.executable, "-m", "deploy.docker.entrypoint"],
            cwd=root,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and proc.poll() is None:
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.TransportError:
                        pass
                    time.sleep(0.1)
                else:
                    pytest.fail((tmp_path / f"{name}.log").read_text())
                yield client
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def test_two_backend_processes_share_bundles_and_uploads(
    gcs_endpoint: str, tmp_path: Path
) -> None:
    bundle = build_agent_bundle(name="shared-artifact-test")
    payload = "shared artifact — hello\n".encode()
    with _server(tmp_path, "a", gcs_endpoint) as a:
        created = a.post(
            "/v1/sessions",
            data={"metadata": "{}"},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
        assert created.status_code == 201, created.text
        sid = created.json()["session_id"]
        with _server(tmp_path, "b", gcs_endpoint) as b:
            read = b.get(f"/v1/sessions/{sid}/agent/contents")
            assert read.status_code == 200, read.text
            assert read.content == bundle
            uploaded = a.post(
                f"/v1/sessions/{sid}/resources/files",
                files={"file": ("hello.txt", payload, "text/plain")},
            )
            assert uploaded.status_code == 201, uploaded.text
            fid = uploaded.json()["id"]
            resource = f"/v1/sessions/{sid}/resources/files/{fid}"
            downloaded = b.get(f"{resource}/content")
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content == payload
        with _server(tmp_path, "replacement", gcs_endpoint) as replacement:
            downloaded = replacement.get(f"{resource}/content")
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content == payload
            deleted = replacement.delete(resource)
            assert deleted.status_code in (200, 204), deleted.text
            assert a.get(f"{resource}/content").status_code == 404
