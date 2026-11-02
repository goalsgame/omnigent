"""E2E: a running runner MCP pool follows tools added to or removed from a server.

Drives :class:`omnigent.runner.mcp_manager.RunnerMcpManager` against a real
MCP subprocess (``tests/tools/fixtures/dynamic_tools_mcp_server.py``) whose
tool list is rewritten while the pool's connection stays open.
"""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.runner import mcp_manager as mcp_manager_module
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.spec.types import AgentSpec, MCPServerConfig

_SERVER = str(Path(__file__).parent / "fixtures" / "dynamic_tools_mcp_server.py")


def _write_tools(state: Path, *names: str) -> None:
    state.write_text(json.dumps({"tools": list(names)}))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture()
def http_server(tmp_path: Path) -> Iterator[tuple[Path, str]]:
    """A stateless streamable-HTTP server listing ``alpha`` and ``gamma``."""
    state = tmp_path / "state.json"
    _write_tools(state, "alpha", "gamma")
    port = _free_port()
    proc = subprocess.Popen([sys.executable, _SERVER, str(state), "http", str(port)])
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                socket.create_connection(("127.0.0.1", port), timeout=1).close()
                break
            except OSError:
                if time.monotonic() > deadline or proc.poll() is not None:
                    raise
                time.sleep(0.1)
        yield state, f"http://127.0.0.1:{port}/mcp"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _http_spec(url: str) -> AgentSpec:
    return AgentSpec(
        spec_version=1,
        mcp_servers=[MCPServerConfig(name="dyn", transport="http", url=url, timeout=20)],
    )


@pytest.mark.asyncio
async def test_next_listing_exposes_added_and_drops_removed_tools(
    http_server: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, url = http_server
    spec = _http_spec(url)
    manager = RunnerMcpManager()
    try:
        first = await manager.schemas_for(spec)
        assert first.failures == {}
        assert first.tool_names == {"dyn__alpha", "dyn__gamma"}
        (server,) = manager._servers.values()
        connection = server.connection

        _write_tools(state, "alpha", "beta")
        within_interval = await manager.schemas_for(spec)
        assert within_interval.tool_names == {"dyn__alpha", "dyn__gamma"}

        monkeypatch.setattr(mcp_manager_module, "_TOOL_REFRESH_INTERVAL_S", 0.0, raising=False)
        refreshed = await manager.schemas_for(spec)
        assert refreshed.tool_names == {"dyn__alpha", "dyn__beta"}
        assert server.connection is connection

        assert await manager.call_tool(spec, "dyn__beta", {"text": "hi"}) == "beta: hi"
        with pytest.raises(RuntimeError, match="no live MCP serving tool"):
            await manager.call_tool(spec, "dyn__gamma", {"text": "hi"})
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_call_to_unlisted_tool_relists_before_failing(
    http_server: tuple[Path, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state, url = http_server
    spec = _http_spec(url)
    manager = RunnerMcpManager()
    try:
        assert (await manager.schemas_for(spec)).tool_names == {"dyn__alpha", "dyn__gamma"}
        _write_tools(state, "alpha", "beta")
        monkeypatch.setattr(mcp_manager_module, "_TOOL_REFRESH_INTERVAL_S", 0.0, raising=False)
        assert await manager.call_tool(spec, "dyn__beta", {"text": "now"}) == "beta: now"
    finally:
        await manager.shutdown()


@pytest.mark.asyncio
async def test_tools_list_changed_notification_forces_relist(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    _write_tools(state, "announce")
    spec = AgentSpec(
        spec_version=1,
        mcp_servers=[
            MCPServerConfig(
                name="dyn",
                transport="stdio",
                command=sys.executable,
                args=[_SERVER, str(state), "stdio"],
                timeout=20,
            )
        ],
    )
    manager = RunnerMcpManager()
    try:
        assert (await manager.schemas_for(spec)).tool_names == {"dyn__announce"}
        await manager.call_tool(spec, "dyn__announce", {"add": "beta"})
        refreshed = await manager.schemas_for(spec)
        assert refreshed.tool_names == {"dyn__announce", "dyn__beta"}
        assert await manager.call_tool(spec, "dyn__beta", {"text": "hi"}) == "beta: hi"
    finally:
        await manager.shutdown()
