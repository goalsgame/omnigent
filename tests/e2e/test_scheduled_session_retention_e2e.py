"""End-to-end test for scheduled-task session retention.

Boots a real ``omnigent server`` with a tiny retention window and sweep
interval, seeds finished scheduled-task runs directly in its database, and
checks that the sweep deletes the unengaged session while the run row stays
with ``conversation_id`` null. A pinned session must survive.

Usage::

    pytest tests/e2e/test_scheduled_session_retention_e2e.py -v
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from omnigent.stores.conversation_store import PINNED_LABEL_KEY
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.scheduled_task_store.sqlalchemy_store import SqlAlchemyScheduledTaskStore
from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e.conftest import _REPO_ROOT, find_free_port
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_RETENTION_SECONDS = 2
_SWEEP_INTERVAL_SECONDS = 1
_DELETE_TIMEOUT_SECONDS = 60


@pytest.fixture
def retention_server(tmp_path: Path) -> Iterator[tuple[str, str]]:
    """Start a server with a 2s retention window; yield ``(base_url, db_uri)``."""
    port = find_free_port()
    db_uri = f"sqlite:///{tmp_path / 'retention.db'}"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    server_log = tmp_path / "server.log"
    config = tmp_path / "server.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "scheduled_tasks": {
                    "session_retention_seconds": _RETENTION_SECONDS,
                    "retention_sweep_interval_seconds": _SWEEP_INTERVAL_SECONDS,
                }
            }
        )
    )
    env = {**os.environ}
    apply_server_env(env, _REPO_ROOT)
    base_url = f"http://localhost:{port}"
    with open(server_log, "w") as log_handle:
        proc = subprocess.Popen(
            [
                server_executable(),
                "-m",
                "omnigent.cli",
                "server",
                "--port",
                str(port),
                "--database-uri",
                db_uri,
                "--artifact-location",
                str(artifact_dir),
                "--config",
                str(config),
            ],
            env=env,
            cwd=compat_server_cwd(),
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        try:
            deadline = time.monotonic() + HEALTH_TIMEOUT_S
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(
                        f"server exited early; log:\n{server_log.read_text()[-3000:]}"
                    )
                try:
                    if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(POLL_INTERVAL_S)
            else:
                raise RuntimeError(f"server not healthy; log:\n{server_log.read_text()[-3000:]}")
            yield base_url, db_uri
        finally:
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)


def test_finished_run_session_is_deleted_and_run_kept(retention_server: tuple[str, str]) -> None:
    base_url, db_uri = retention_server
    conversations = SqlAlchemyConversationStore(db_uri)
    tasks = SqlAlchemyScheduledTaskStore(db_uri)
    task_id = uuid.uuid4().hex
    tasks.create(
        scheduled_task_id=task_id,
        name="retention",
        prompt="p",
        rrule="FREQ=DAILY",
        user_id=None,
        agent_id=uuid.uuid4().hex,
        timezone="UTC",
        state="paused",
    )
    expired = conversations.create_conversation()
    pinned = conversations.create_conversation()
    conversations.set_labels(pinned.id, {f"{PINNED_LABEL_KEY}.local": "1"})
    now = int(time.time())
    expired_run, pinned_run = uuid.uuid4().hex, uuid.uuid4().hex
    tasks.create_run(
        expired_run, task_id, "succeeded", now, conversation_id=expired.id, finished_at=now
    )
    tasks.create_run(
        pinned_run, task_id, "succeeded", now, conversation_id=pinned.id, finished_at=now
    )

    deadline = time.monotonic() + _DELETE_TIMEOUT_SECONDS
    while True:
        runs = {r.id: r for r in tasks.list_runs(task_id)[0]}
        if (
            conversations.get_conversation(expired.id) is None
            and runs[expired_run].conversation_id is None
        ):
            break
        assert time.monotonic() < deadline, "expired session was not deleted and unlinked"
        time.sleep(POLL_INTERVAL_S)

    assert runs[expired_run].conversation_id is None
    assert runs[expired_run].status == "succeeded"
    assert runs[pinned_run].conversation_id == pinned.id
    assert conversations.get_conversation(pinned.id) is not None
    assert httpx.get(f"{base_url}/health", timeout=5).status_code == 200
