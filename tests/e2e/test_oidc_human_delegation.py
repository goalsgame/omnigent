"""Real HTTP server and HTTPS issuer: delegated and browser sessions share an owner."""

from __future__ import annotations

import datetime
import json
import os
import socket
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import jwt
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from tests.server.helpers import build_agent_bundle


def test_delegated_session_is_owned_by_browser_user(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path, key_path = tmp_path / "ca.pem", tmp_path / "key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    state = {"nonce": ""}
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk.update(kid="issuer-key", alg="RS256", use="sig")

    def signed(**extra):
        issued = int(time.time())
        return jwt.encode(
            {
                "iss": issuer,
                "sub": "human-subject",
                "iat": issued,
                "exp": issued + 300,
                "email": " Person@Example.Test ",
                "email_verified": True,
                "preferred_username": "person",
                **extra,
            },
            key,
            algorithm="RS256",
            headers={"kid": "issuer-key"},
        )

    class IssuerHandler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def reply(self, value):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(value).encode())

        def do_GET(self):
            path = urlsplit(self.path)
            if path.path == "/.well-known/openid-configuration":
                self.reply(
                    {
                        "issuer": issuer,
                        "authorization_endpoint": issuer + "/authorize",
                        "token_endpoint": issuer + "/token",
                        "jwks_uri": issuer + "/keys",
                    }
                )
            elif path.path == "/keys":
                self.reply({"keys": [jwk]})
            elif path.path == "/authorize":
                query = parse_qs(path.query)
                state["nonce"] = query.get("nonce", [""])[0]
                self.send_response(302)
                self.send_header(
                    "Location",
                    query["redirect_uri"][0]
                    + "?"
                    + urlencode(
                        {
                            "code": "synthetic-code",
                            "state": query["state"][0],
                        }
                    ),
                )
                self.end_headers()
            else:
                self.send_error(404)

        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", "0")))
            self.reply(
                {
                    "access_token": "synthetic-unused",
                    "token_type": "Bearer",
                    "id_token": signed(aud="web", nonce=state["nonce"]),
                }
            )

    issuer_server = ThreadingHTTPServer(("127.0.0.1", 0), IssuerHandler)
    issuer = f"https://localhost:{issuer_server.server_port}"
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(cert_path, key_path)
    issuer_server.socket = tls.wrap_socket(issuer_server.socket, server_side=True)
    issuer_thread = threading.Thread(target=issuer_server.serve_forever, daemon=True)
    issuer_thread.start()
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    base_url = f"http://127.0.0.1:{port}"
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "oidc_human_auth": {
                    "audience": "agent-api",
                    "scope": "agent-access",
                    "clients": ["tool-exchange"],
                }
            }
        )
    )
    repo = Path(__file__).resolve().parents[2]
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env.update(
        {
            "PYTHONPATH": os.pathsep.join(
                [str(repo), str(repo / "sdks/python-client"), str(repo / "sdks/ui")]
            ),
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "SSL_CERT_FILE": str(cert_path),
            "OMNIGENT_CONFIG": str(config),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config-home"),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_AUTH_PROVIDER": "oidc",
            "OMNIGENT_OIDC_ISSUER": issuer,
            "OMNIGENT_OIDC_CLIENT_ID": "web",
            "OMNIGENT_OIDC_CLIENT_SECRET": "synthetic-secret",
            "OMNIGENT_OIDC_COOKIE_SECRET": "11" * 32,
            "OMNIGENT_OIDC_REDIRECT_URI": base_url + "/auth/callback",
            "OMNIGENT_OIDC_ALLOWED_DOMAINS": "example.test",
        }
    )
    process = None
    log_path = tmp_path / "server.log"
    try:
        with log_path.open("w") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "omnigent.cli",
                    "server",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(port),
                    "--database-uri",
                    f"sqlite:///{tmp_path}/test.db",
                    "--artifact-location",
                    str(tmp_path / "artifacts"),
                ],
                env=env,
                cwd=repo,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            trust = ssl.create_default_context(cafile=str(cert_path))
            with httpx.Client(base_url=base_url, verify=trust, timeout=10) as client:
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    assert process.poll() is None, log_path.read_text()[-3000:]
                    try:
                        if client.get("/health").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise AssertionError(
                        "Server never became healthy: " + log_path.read_text()[-3000:]
                    )
                login = client.get("/auth/login", follow_redirects=True)
                assert login.status_code == 200, login.text
                assert "ap_session" in client.cookies
                headers = {
                    "Authorization": "Bearer "
                    + signed(
                        aud="agent-api", azp="tool-exchange", typ="Bearer", scope="agent-access"
                    )
                }
                # Use a separate client: the browser cookie must not mask the delegated token.
                with httpx.Client(base_url=base_url, timeout=10, headers=headers) as delegated:
                    created = delegated.post(
                        "/v1/sessions",
                        data={"metadata": "{}"},
                        files={
                            "bundle": (
                                "agent.tar.gz",
                                build_agent_bundle(name="delegated"),
                                "application/gzip",
                            )
                        },
                    )
                    assert created.status_code == 201, created.text
                    session_id = created.json()["session_id"]
                    owner = delegated.get(f"/v1/sessions/{session_id}/owner")
                    assert owner.json() == {"owner": "person@example.test"}
                snapshot = client.get(f"/v1/sessions/{session_id}")
                assert snapshot.status_code == 200, snapshot.text
                renamed = client.patch(
                    f"/v1/sessions/{session_id}", json={"title": "Browser continues"}
                )
                assert renamed.status_code == 200, renamed.text
                deleted = client.delete(f"/v1/sessions/{session_id}")
                assert deleted.status_code == 200, deleted.text
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)
        issuer_server.shutdown()
        issuer_server.server_close()
        issuer_thread.join(timeout=5)
