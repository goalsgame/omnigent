"""Generated workspace preparation, owner broker, wrapper and retained workspace.

Uses a loopback credential API and an executable Lore test double; no cluster,
external credentials or provider inference is required.
"""

import os
import shlex
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from omnigent import lore_checkout
from omnigent.host.identity import HOST_TOKEN_ENV_VAR
from omnigent.onboarding.sandboxes.kubernetes import _render_workspace_prep_command
from omnigent.server.lore import LoreConfig, LoreDelegationBroker
from omnigent.server.managed_hosts import parse_repo_workspace
from omnigent.server.routes.host_credentials import create_host_credentials_router
from tests.server import test_oidc_mcp
from tests.server.routes.test_host_credentials import _FakeHostStore


def test_lore_checkout_wake_and_command_renewal(tmp_path: Path, db_uri, monkeypatch):
    base, _claims, exchanges = test_oidc_mcp.broker.__wrapped__(db_uri, monkeypatch)
    remote = "lores://lore.example.test/game"
    config = LoreConfig(base.config, {remote: "/Content\n"}, ())
    broker = LoreDelegationBroker(config, base.oidc, base.store, base.auth, base.client)
    broker.capture("alice@example.com", "person-sub", "original")
    app = FastAPI()
    app.state.lore_identity_broker = broker
    app.include_router(
        create_host_credentials_router(
            _FakeHostStore("host1", "launch-token", "alice@example.com")
        ),
        prefix="/v1",
    )
    client = TestClient(app)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            response = client.get(self.path, headers=dict(self.headers))
            self.send_response(response.status_code)
            self.send_header("Content-Length", str(len(response.content)))
            self.end_headers()
            self.wfile.write(response.content)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    home = tmp_path / "home"
    home.mkdir()
    binary = tmp_path / "bin/lore"
    binary.parent.mkdir()
    binary.write_text(f"""#!{sys.executable}
import json, sys
from pathlib import Path
args = sys.argv[1:]
assert args[:2] == ['--identity-token', 'access']
if 'clone' in args:
    dest = Path(args[-1])
    (dest / '.lore').mkdir(parents=True)
    (dest / 'hello.txt').write_text('hello')
    view = Path(args[args.index('--view')+1]).read_text()
    assert view == '/Content\\n'
    assert args[args.index('--revision')+1] in ('main@369', '369', 'main@latest')
else:
    assert args[-1] == 'status'
print('ok')
""")
    binary.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", str(binary.parent) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-token")
    workspace = home / "workspace"
    repo = parse_repo_workspace(remote + "#main@369")
    command = _render_workspace_prep_command(
        str(workspace), [repo], server_url=url, host_id="host1"
    )
    script = command[-1].replace("python3 -I", shlex.quote(sys.executable) + " -I")
    try:
        first = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert first.returncode == 0, first.stderr
        checkout = workspace / "game"
        assert (checkout / "hello.txt").read_text() == "hello"
        (checkout / "hello.txt").write_text("unsaved work")
        second = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert second.returncode == 0, second.stderr
        assert (checkout / "hello.txt").read_text() == "unsaved work"
        relocated = tmp_path / "new-image/bin/lore"
        relocated.parent.mkdir(parents=True)
        binary.rename(relocated)
        monkeypatch.setenv(
            "PATH",
            os.pathsep.join(
                [str(lore_checkout._home() / "bin"), str(relocated.parent), os.environ["PATH"]]
            ),
        )
        wake = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
        assert wake.returncode == 0, wake.stderr
        assert (checkout / "hello.txt").read_text() == "unsaved work"
        lore_checkout.configure(url, "host1")
        config_text = (lore_checkout._home() / "host.json").read_text()
        assert "launch-token" not in config_text and "access" not in config_text
        env = dict(os.environ)
        env.pop(HOST_TOKEN_ENV_VAR)
        # CLI-created copies outside the original checkout must renew too.
        for relative, prefix, flag, ref in (
            ("copy", ["clone"], "--revision", "main@369"),
            ("nested/copy", ["clone"], "--revision", "369"),
            ("branch-copy", ["repository", "clone"], "--branch", "main"),
        ):
            clone_args = (
                [*prefix, remote, relative, flag, ref]
                if relative == "copy"
                else [*prefix, remote, flag, ref, relative]
            )
            copied = subprocess.run(
                [str(lore_checkout._home() / "bin/lore"), *clone_args],
                cwd=home,
                env=env,
                capture_output=True,
                text=True,
            )
            assert copied.returncode == 0, copied.stderr
            status = subprocess.run(
                [str(lore_checkout._home() / "bin/lore"), "status"],
                cwd=home / relative,
                env=env,
                capture_output=True,
                text=True,
            )
            assert status.returncode == 0, status.stderr
        assert len(list((lore_checkout._home() / "checkouts").glob("*.json"))) == 4
        for _ in range(2):
            current = broker.store.get("alice@example.com", "oidc_lore", with_secret=True)
            broker.store.update_secret(
                "alice@example.com", "oidc_lore", secret={**current.secret, "expires_at": 0}
            )
            status = subprocess.run(
                [str(lore_checkout._home() / "bin/lore"), "status"],
                cwd=checkout,
                env=env,
                capture_output=True,
                text=True,
            )
            assert status.returncode == 0, status.stderr
        assert len(exchanges) == 3
        assert exchanges[-1]["grant_type"] == ["refresh_token"]
        current = broker.store.get("alice@example.com", "oidc_lore", with_secret=True)
        broker.store.update_secret(
            "alice@example.com", "oidc_lore", secret={**current.secret, "expires_at": 0}
        )
        broker.client = httpx.Client(
            transport=httpx.MockTransport(lambda request: httpx.Response(400))
        )
        denied = subprocess.run(
            [str(lore_checkout._home() / "bin/lore"), "status"],
            cwd=checkout,
            env=env,
            capture_output=True,
            text=True,
        )
        assert denied.returncode != 0
        assert "renew delegation" in denied.stderr
        assert "launch-token" not in denied.stderr and "--identity-token" not in denied.stderr

    finally:
        server.shutdown()
        server.server_close()
        client.close()
