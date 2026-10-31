"""Tests for :mod:`omnigent.onboarding.sandboxes.microsandbox_remote`."""

from __future__ import annotations

import json

import click
import httpx
import pytest

from omnigent.onboarding.sandboxes import microsandbox_remote as remote
from omnigent.onboarding.sandboxes.base import DEFAULT_HOST_IMAGE, SandboxGoneError
from omnigent.onboarding.sandboxes.microsandbox_remote import (
    DEFAULT_TOKEN_ENV,
    RemoteMicrosandboxLauncher,
    sandbox_name,
)
from omnigent.server.managed_hosts import (
    MICROSANDBOX_REMOTE_MANAGED_TOKEN_TTL_S,
    parse_sandbox_config,
)

ENDPOINT = "http://sandbox-host:8740"


class FakeHost:
    """In-memory sandbox host API recording every request it receives."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.sandboxes: dict[str, str] = {}
        self.exec_response: dict[str, object] = {
            "exit_code": 0,
            "stdout": "ok\n",
            "stderr": "",
            "timed_out": False,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = request.url.path.strip("/").split("/")
        if parts == ["v1", "host"]:
            return httpx.Response(200, json={"cpus": 32})
        name = parts[2]
        action = parts[3] if len(parts) > 3 else None
        if request.method == "PUT":
            self.sandboxes[name] = "running"
            return httpx.Response(200, json={"name": name, "created": True, "status": "running"})
        if name not in self.sandboxes:
            if request.method == "DELETE":
                return httpx.Response(204)
            return httpx.Response(404, json={"detail": f"sandbox {name} not found"})
        if request.method == "GET":
            return httpx.Response(200, json={"name": name, "status": self.sandboxes[name]})
        if request.method == "DELETE":
            del self.sandboxes[name]
            return httpx.Response(204)
        if action == "start":
            self.sandboxes[name] = "running"
            return httpx.Response(200, json={"name": name, "status": "running"})
        if action == "touch":
            return httpx.Response(204 if self.sandboxes[name] == "running" else 409)
        if action == "exec":
            return httpx.Response(200, json=self.exec_response)
        return httpx.Response(400)


@pytest.fixture
def host() -> FakeHost:
    return FakeHost()


@pytest.fixture
def launcher(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> RemoteMicrosandboxLauncher:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "static-token")
    monkeypatch.setenv("GIT_TOKEN", "ghp_test")
    return RemoteMicrosandboxLauncher(
        endpoint=ENDPOINT + "/",
        cpus=30,
        memory_mib=4096,
        max_memory_mib=16384,
        idle_timeout_s=900,
        env=["GIT_TOKEN", "UNSET_VAR"],
        workspace_path="/home/omnigent",
        workspace_quota_mib=51200,
        transport=httpx.MockTransport(host),
    )


def test_sandbox_names_are_valid_and_unique() -> None:
    first, second = sandbox_name("Managed_A1B2!"), sandbox_name("Managed_A1B2!")
    assert first != second
    assert first.startswith("managed-a1b2-")
    long = sandbox_name("x" * 200)
    assert len(long) <= 63
    assert sandbox_name("___").startswith("sandbox-")


def test_provision_creates_a_sized_sandbox_with_workspace_and_env(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    launcher.prepare()
    sandbox_id = launcher.provision("managed-a1b2c3d4")
    put = host.requests[-1]
    assert put.method == "PUT"
    assert put.url.path == f"/v1/sandboxes/{sandbox_id}"
    assert put.headers["Authorization"] == "Bearer static-token"
    assert json.loads(put.content) == {
        "image": DEFAULT_HOST_IMAGE,
        "cpus": 30,
        "memory_mib": 4096,
        "max_memory_mib": 16384,
        "idle_timeout_s": 900,
        "env": {"GIT_TOKEN": "ghp_test", "HOME": "/home/omnigent"},
        "workspace_path": "/home/omnigent",
        "workspace_quota_mib": 51200,
    }


def test_run_returns_output_and_raises_on_failure_when_checked(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    sandbox_id = launcher.provision("managed")
    result = launcher.run(sandbox_id, "echo ok")
    assert (result.returncode, result.stdout) == (0, "ok\n")
    assert json.loads(host.requests[-1].content)["command"] == "echo ok"
    host.exec_response = {"exit_code": 3, "stdout": "", "stderr": "boom", "timed_out": False}
    assert launcher.run(sandbox_id, "false", check=False).returncode == 3
    with pytest.raises(click.ClickException, match="boom"):
        launcher.run(sandbox_id, "false")
    host.exec_response = {
        "exit_code": None,
        "stdout": "",
        "stderr": "timed out",
        "timed_out": True,
    }
    assert launcher.run(sandbox_id, "sleep 999", check=False).returncode == -1


def test_lifecycle_maps_missing_sandboxes(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    sandbox_id = launcher.provision("managed")
    assert launcher.is_running(sandbox_id) is True
    assert launcher.keep_alive(sandbox_id) is True
    host.sandboxes[sandbox_id] = "stopped"
    assert launcher.is_running(sandbox_id) is False
    assert launcher.keep_alive(sandbox_id) is False
    launcher.resume(sandbox_id)
    assert host.sandboxes[sandbox_id] == "running"
    launcher.terminate(sandbox_id)
    launcher.terminate(sandbox_id)
    assert launcher.is_running(sandbox_id) is False
    with pytest.raises(SandboxGoneError):
        launcher.resume(sandbox_id)
    with pytest.raises(SandboxGoneError):
        launcher.run(sandbox_id, "true")


def test_lost_create_response_retries_the_same_name(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    calls = {"put": 0}

    def flaky(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            calls["put"] += 1
            if calls["put"] == 1:
                host(request)
                raise httpx.ReadTimeout("response lost", request=request)
        return host(request)

    launcher._transport = httpx.MockTransport(flaky)
    sandbox_id = launcher.provision("managed")
    puts = [r for r in host.requests if r.method == "PUT"]
    assert len(puts) == 2
    assert {r.url.path for r in puts} == {f"/v1/sandboxes/{sandbox_id}"}
    assert host.sandboxes[sandbox_id] == "running"


def test_failed_create_removes_a_possibly_created_sandbox(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            host(request)
            raise httpx.ReadTimeout("response lost", request=request)
        return host(request)

    launcher._transport = httpx.MockTransport(broken)
    with pytest.raises(click.ClickException, match="response lost"):
        launcher.provision("managed")
    deletes = [r for r in host.requests if r.method == "DELETE"]
    puts = [r for r in host.requests if r.method == "PUT"]
    assert len(puts) == 3
    assert [r.url.path for r in deletes] == [puts[0].url.path]
    assert host.sandboxes == {}


def test_rejected_retry_after_a_lost_response_removes_the_sandbox(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    calls = {"put": 0}

    def lost_then_throttled(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            calls["put"] += 1
            if calls["put"] == 1:
                host(request)
                raise httpx.ReadTimeout("response lost", request=request)
            return httpx.Response(429, json={"detail": "too many boots"})
        return host(request)

    launcher._transport = httpx.MockTransport(lost_then_throttled)
    with pytest.raises(click.ClickException, match="429"):
        launcher.provision("managed")
    assert calls["put"] == 2
    assert [r.method for r in host.requests if r.method == "DELETE"] == ["DELETE"]
    assert host.sandboxes == {}


def test_id_token_failures_become_lifecycle_errors(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(_request: object, _audience: str) -> str:
        raise ValueError("metadata server unavailable")

    pytest.importorskip("google.oauth2.id_token")
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", fail)
    monkeypatch.setattr(remote, "_credentials", {})
    launcher = RemoteMicrosandboxLauncher(
        endpoint=ENDPOINT, audience="https://sandbox-host", transport=httpx.MockTransport(host)
    )
    assert launcher.is_running("any") is None
    assert launcher.keep_alive("any") is False
    with pytest.raises(click.ClickException, match="metadata server unavailable"):
        launcher.prepare()


def test_requires_a_token(host: FakeHost, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(DEFAULT_TOKEN_ENV, raising=False)
    launcher = RemoteMicrosandboxLauncher(endpoint=ENDPOINT, transport=httpx.MockTransport(host))
    with pytest.raises(click.ClickException, match=DEFAULT_TOKEN_ENV):
        launcher.prepare()
    assert host.requests == []


def test_audience_uses_a_cached_google_id_token(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def fetch_id_token(_request: object, audience: str) -> str:
        calls.append(audience)
        return "id-token"

    pytest.importorskip("google.oauth2.id_token")
    monkeypatch.setattr("google.oauth2.id_token.fetch_id_token", fetch_id_token)
    monkeypatch.setattr(remote, "_credentials", {})
    launcher = RemoteMicrosandboxLauncher(
        endpoint=ENDPOINT, audience="https://sandbox-host", transport=httpx.MockTransport(host)
    )
    launcher.prepare()
    launcher.prepare()
    assert calls == ["https://sandbox-host"]
    assert host.requests[-1].headers["Authorization"] == "Bearer id-token"


def test_parse_config_builds_the_remote_launcher() -> None:
    cfg = parse_sandbox_config(
        {
            "provider": "microsandbox_remote",
            "server_url": "https://omnigent.example.com",
            "microsandbox_remote": {
                "endpoint": ENDPOINT,
                "token_env": "SHIM_TOKEN",
                "image": "registry.example/omnigent-host@sha256:abc",
                "env": ["GIT_TOKEN"],
                "cpus": 30,
                "memory_mib": 4096,
                "max_memory_mib": 16384,
                "idle_timeout_s": 0,
                "workspace_path": "/home/omnigent",
                "workspace_quota_mib": 51200,
                "user": "sandbox",
            },
        }
    )
    assert cfg is not None
    cfg = cfg.default
    assert cfg.provider == "microsandbox_remote"
    assert cfg.managed_launch_supported is True
    assert cfg.token_ttl_s == MICROSANDBOX_REMOTE_MANAGED_TOKEN_TTL_S
    launcher = cfg.launcher_factory()
    assert isinstance(launcher, RemoteMicrosandboxLauncher)
    assert launcher.can_resume is True
    assert launcher.capabilities.resume_stopped is True
    assert launcher.capabilities.cli_bootstrap is False


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ({}, "endpoint"),
        ({"endpoint": ENDPOINT, "bogus": 1}, "bogus"),
        ({"endpoint": ENDPOINT, "cpus": 0}, "cpus"),
        ({"endpoint": ENDPOINT, "idle_timeout_s": -1}, "idle_timeout_s"),
        ({"endpoint": ENDPOINT, "idle_timeout_s": 60}, "two keepalive refresh intervals"),
    ],
)
def test_parse_config_rejects_invalid_sections(section: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse_sandbox_config(
            {
                "provider": "microsandbox_remote",
                "server_url": "https://omnigent.example.com",
                "microsandbox_remote": section,
            }
        )


def _exec_bodies(host: FakeHost) -> list[dict[str, object]]:
    return [json.loads(r.content) for r in host.requests if r.url.path.endswith("/exec")]


def test_github_clone_uses_the_brokered_helper_with_the_token_in_env(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    from omnigent.host.identity import HOST_TOKEN_ENV_VAR
    from omnigent.onboarding.sandboxes.types import RepoWorkspace

    host.exec_response = {"exit_code": 0, "stdout": "/root", "stderr": "", "timed_out": False}
    sandbox_id = launcher.provision("managed")
    launcher.start_host(
        sandbox_id,
        token="launch-secret",
        host_id="host-1",
        host_name="managed",
        server_url="https://omnigent.example",
        repos=[
            RepoWorkspace(
                url="https://github.com/org/private.git", branch=None, repo_name="private"
            ),
            RepoWorkspace(
                url="https://gitlab.com/org/public.git", branch=None, repo_name="public"
            ),
        ],
    )
    clones = [b for b in _exec_bodies(host) if "git clone" in str(b["command"])]
    assert len(clones) == 2
    github, other = clones
    assert github["env"] == {HOST_TOKEN_ENV_VAR: "launch-secret"}
    assert "launch-secret" not in str(github["command"])
    assert "omnigent.git_credential_github" in str(github["command"])
    assert "env" not in other
    assert "git_credential_github" not in str(other["command"])
    assert launcher._clone_auth == {}


@pytest.mark.parametrize(
    ("credential", "drop_writes", "exit_code", "expected"),
    [
        ({"connected": True, "token": "gh"}, False, 0, "helper"),
        ({"connected": True, "token": "gh"}, True, 1, ""),
        ({"connected": False, "reason": "not_connected"}, False, 1, "unconnected"),
        ({"connected": False, "reason": "disabled"}, False, 0, "guest-config"),
    ],
)
def test_clone_script_wires_a_verified_temporary_helper(
    tmp_path, credential, drop_writes, exit_code, expected
) -> None:
    import http.server
    import os
    import shutil
    import subprocess
    import sys
    import threading

    from omnigent.host.identity import HOST_TOKEN_ENV_VAR
    from omnigent.host.workspace_errors import (
        GITHUB_CHECKOUT_UNCONNECTED,
        WORKSPACE_ERROR_MESSAGES,
    )

    seen: list[str | None] = []

    class Broker(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen.append(self.headers.get("X-Omnigent-Host-Token"))
            body = json.dumps(credential).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: object) -> None:
            pass

    home, scratch, bin_dir = tmp_path / "home", tmp_path / "tmp", tmp_path / "bin"
    for directory in (home, scratch, bin_dir):
        directory.mkdir()
    (home / ".gitconfig").write_text("[guest]\n\tmarker = kept\n")
    path = f"{os.path.dirname(sys.executable)}:{os.environ['PATH']}"
    if drop_writes:
        real_git = shutil.which("git")
        wrapper = bin_dir / "git"
        wrapper.write_text(
            f'#!/bin/sh\ncase " $* " in *" --add "*) exit 0 ;; esac\nexec {real_git} "$@"\n'
        )
        wrapper.chmod(0o755)
        path = f"{bin_dir}:{path}"
    key = remote._GITHUB_HELPER_KEY
    clone = f"git config --global --get-all {key}; git config --global guest.marker || true"
    if expected == "unconnected":
        clone = "false"
    server = http.server.HTTPServer(("127.0.0.1", 0), Broker)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        script = remote._github_clone_script(
            f"http://127.0.0.1:{server.server_port}", "host-1", clone
        )
        env = {
            "PATH": path,
            "HOME": str(home),
            "TMPDIR": str(scratch),
            HOST_TOKEN_ENV_VAR: "launch-secret",
        }
        result = subprocess.run(
            ["sh", "-c", script], env=env, capture_output=True, text=True, check=False
        )
    finally:
        server.shutdown()
    assert seen == ["launch-secret"]
    assert result.returncode == exit_code, result.stderr
    assert list(scratch.iterdir()) == []
    if expected == "helper":
        helpers = result.stdout.splitlines()
        assert helpers[0] == ""
        assert "git_credential_github" in helpers[1]
        assert "launch-secret" not in result.stdout
        assert "kept" not in result.stdout
    elif expected == "unconnected":
        assert WORKSPACE_ERROR_MESSAGES[GITHUB_CHECKOUT_UNCONNECTED] in result.stderr
    elif expected == "guest-config":
        assert result.stdout.strip() == "kept"
    else:
        assert result.stdout == ""


def test_git_clone_policy_reaches_the_github_clone(
    launcher: RemoteMicrosandboxLauncher, host: FakeHost
) -> None:
    from omnigent.onboarding.sandboxes.types import GitCloneOptions, RepoWorkspace

    assert launcher.capabilities.git_clone_options
    host.exec_response = {"exit_code": 0, "stdout": "/root", "stderr": "", "timed_out": False}
    sandbox_id = launcher.provision("managed")
    launcher.start_host(
        sandbox_id,
        token="launch-secret",
        host_id="host-1",
        host_name="managed",
        server_url="https://omnigent.example",
        repos=[
            RepoWorkspace(
                url="https://github.com/org/private.git",
                branch="main",
                repo_name="private",
                git_clone=GitCloneOptions(depth=1),
            )
        ],
    )
    (clone,) = [b for b in _exec_bodies(host) if "git clone" in str(b["command"])]
    assert "--depth 1" in str(clone["command"])


def test_a_guest_user_is_requested_on_create(
    host: FakeHost, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "static-token")
    launcher = RemoteMicrosandboxLauncher(
        endpoint=ENDPOINT,
        workspace_path="/home/omnigent",
        user="sandbox",
        transport=httpx.MockTransport(host),
    )
    launcher.provision("managed")
    body = json.loads(host.requests[-1].content)
    assert body["user"] == "sandbox"
    assert body["env"]["HOME"] == "/home/omnigent"


def test_an_https_endpoint_trusts_the_configured_ca(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import datetime
    import http.server
    import ipaddress
    import ssl
    import threading

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID

    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "sandbox-host")])
    now = datetime.datetime.now(datetime.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(minutes=1))
        .not_valid_after(now + datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    pem = cert.public_bytes(serialization.Encoding.PEM).decode()
    (tmp_path / "cert.pem").write_text(pem)
    (tmp_path / "key.pem").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )

    class Host(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"cpus": 32}')

        def log_message(self, *_args: object) -> None:
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Host)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(tmp_path / "cert.pem", tmp_path / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv(DEFAULT_TOKEN_ENV, "static-token")
    endpoint = f"https://127.0.0.1:{server.server_port}"
    try:
        RemoteMicrosandboxLauncher(endpoint=endpoint, ca_cert=pem).prepare()
        with pytest.raises(click.ClickException, match="CERTIFICATE_VERIFY_FAILED"):
            RemoteMicrosandboxLauncher(endpoint=endpoint).prepare()
    finally:
        server.shutdown()
