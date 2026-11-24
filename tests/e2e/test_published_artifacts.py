"""Native publication discovery and HTTP delivery through a real server process."""

import json
import os
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from omnigent.runner.tool_dispatch import build_native_relay_tool_schemas, execute_tool
from omnigent.spec.types import AgentSpec
from tests.e2e.conftest import (
    create_runner_bound_session,
    find_free_port,
    lookup_agent_id,
    register_inline_agent,
)

_REPO = Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
async def test_native_publication_survives_source_session_deletion(
    http_client: httpx.Client, live_runner_id: str, tmp_path
):
    """A resolved native spec discovers the tool and publishes immutable workspace bytes."""
    name = register_inline_agent(
        http_client,
        name=f"publisher-{uuid.uuid4().hex[:8]}",
        harness="pi-native",
        model="test-model",
        profile="",
        prompt="Publish reports.",
    )
    sid = create_runner_bound_session(http_client, agent_name=name, runner_id=live_runner_id)
    spec = AgentSpec(spec_version=1)
    assert "sys_publish_artifact" in {
        tool["name"] for tool in build_native_relay_tool_schemas(spec)
    }
    content = b"<!doctype html><h1>Published from a native session</h1>"
    (tmp_path / "report.html").write_bytes(content)
    publication = None
    try:
        async with httpx.AsyncClient(
            base_url=str(http_client.base_url), headers=dict(http_client.headers), trust_env=False
        ) as api:
            result = await execute_tool(
                tool_name="sys_publish_artifact",
                arguments=json.dumps(
                    {"path": "report.html", "title": "Native report", "kind": "html"}
                ),
                server_client=api,
                agent_spec=spec,
                conversation_id=sid,
                runner_workspace=tmp_path,
                effective_harness="pi-native",
            )
            publication = json.loads(result)
            assert "id" in publication, result
            first = await api.get(publication["download_url"])
            assert first.status_code == 200
            assert first.content == content
            preview = await api.get(f"/v1/artifacts/{publication['id']}/preview")
            assert preview.content == content
            assert preview.headers["content-type"].startswith("text/plain")
            assert preview.headers["content-disposition"].startswith("attachment;")
            assert preview.headers["content-security-policy"] == "default-src 'none'; sandbox"
            async with httpx.AsyncClient(
                base_url=str(http_client.base_url), trust_env=False
            ) as public:
                renderer = await public.get("/v1/artifact-renderer")
                assert renderer.status_code == 200
                assert "frame-src 'none'" in renderer.headers["content-security-policy"]
                assert content not in renderer.content
            deleted = await api.delete(f"/v1/sessions/{sid}")
            deleted.raise_for_status()
            after = await api.get(publication["download_url"])
            assert after.status_code == 200
            assert after.content == content
            metadata = await api.get(f"/v1/artifacts/{publication['id']}")
            assert metadata.json()["source_session_id"] == sid
    finally:
        if publication and "id" in publication:
            http_client.delete(f"/v1/artifacts/{publication['id']}").raise_for_status()
        http_client.delete(f"/v1/sessions/{sid}")


def _healthy(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=2).status_code == 200
    except httpx.HTTPError:
        return False


@contextmanager
def _isolated_server(state: Path, config: dict) -> Iterator[httpx.Client]:
    port = find_free_port()
    config_path = state / f"server-{port}.yaml"
    config_path.write_text(yaml.safe_dump(config))
    log = open(state / f"server-{port}.log", "w")  # noqa: SIM115
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{state / 'publications.db'}",
            "--artifact-location",
            str(state / "artifacts"),
            "--config",
            str(config_path),
        ],
        cwd=str(_REPO),
        env={**os.environ, "OMNIGENT_SKIP_ONBOARD": "1", "OMNIGENT_NO_UPDATE_CHECK": "1"},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        deadline = time.monotonic() + 90
        while not _healthy(port):
            if proc.poll() is not None or time.monotonic() > deadline:
                log.flush()
                raise RuntimeError(
                    "isolated server did not start:\n"
                    + (state / f"server-{port}.log").read_text()[-3000:]
                )
            time.sleep(0.2)
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            timeout=60,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            trust_env=False,
        ) as client:
            yield client
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()


def _grant(client: httpx.Client, artifact_id: str, group: str) -> httpx.Response:
    revision = client.get(f"/v1/artifacts/{artifact_id}").json()["sharing_revision"]
    return client.put(
        f"/v1/artifacts/{artifact_id}/permissions",
        json={"revision": revision, "grants": [{"principal_type": "group", "principal": group}]},
    )


def test_disabled_publishing_keeps_existing_links_after_restart(tmp_path):
    """A server restarted with publishing disabled serves old links but takes no new ones."""
    (tmp_path / "artifacts").mkdir()
    content = b"<!doctype html><h1>Published before the switch</h1>"
    upload = {"file": ("report.html", content)}
    form = {"title": "Report", "kind": "html"}
    with _isolated_server(tmp_path, {}) as server:
        assert server.get("/v1/info").json()["published_artifacts_enabled"] is True
        name = register_inline_agent(
            server,
            name=f"publisher-{uuid.uuid4().hex[:8]}",
            harness="pi-native",
            model="test-model",
            profile="",
            prompt="Publish reports.",
        )
        created = server.post("/v1/sessions", json={"agent_id": lookup_agent_id(server, name)})
        created.raise_for_status()
        sid = created.json()["id"]
        published = server.post(f"/v1/sessions/{sid}/artifacts", data=form, files=upload)
        assert published.status_code == 201, published.text
        publication = published.json()
        assert _grant(server, publication["id"], "engineering").status_code == 200

    with _isolated_server(tmp_path, {"published_artifacts": {"enabled": False}}) as server:
        assert server.get("/v1/info").json()["published_artifacts_enabled"] is False
        refused = server.post(f"/v1/sessions/{sid}/artifacts", data=form, files=upload)
        assert refused.status_code == 403
        assert refused.json()["detail"] == "Artifact publishing is disabled on this server"
        assert _grant(server, publication["id"], "design").status_code == 403
        download = server.get(f"/v1/artifacts/{publication['id']}/content")
        assert download.status_code == 200
        assert download.content == content
        assert [entry["id"] for entry in server.get("/v1/artifacts").json()["data"]] == [
            publication["id"]
        ]
