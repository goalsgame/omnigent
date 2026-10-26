"""Uvicorn serving with the omnigent server's graceful-shutdown sequence.

Every launcher that serves ``create_app`` in production (``omnigent server`` and
the Docker entrypoint) builds its server here, so each one marks the shutdown
before runner tunnels close, drains live session streams, and bounds the wait
for open connections.
"""

from __future__ import annotations

import os
import socket
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import uvicorn

# How long uvicorn waits for active connections (WebSocket, SSE) after
# SIGTERM before force-closing them. SSE streams signal themselves via
# session_stream.shutdown_all() in the server's shutdown(), so the main
# remaining consumers of this window are WebSocket tunnels that need a moment
# to drain. 5 s is enough for a clean tunnel teardown while keeping Ctrl-C
# feeling instant and staying well inside a container's termination grace.
GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT = 5


def graceful_shutdown_timeout_s() -> int:
    """Return the graceful-shutdown wait for open connections, in seconds.

    :returns: ``OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S`` when set, for deployments
        that need a longer drain window (e.g. large file uploads); otherwise
        :data:`GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT`.
    """
    return int(
        os.environ.get(
            "OMNIGENT_SERVER_SHUTDOWN_TIMEOUT_S",
            str(GRACEFUL_SHUTDOWN_TIMEOUT_S_DEFAULT),
        )
    )


def shutdown_signaling_server(config: uvicorn.Config) -> uvicorn.Server:
    """Build a uvicorn server that signals omnigent before its shutdown wait.

    :param config: The uvicorn configuration to serve. Callers set
        ``timeout_graceful_shutdown`` from :func:`graceful_shutdown_timeout_s`;
        uvicorn otherwise waits for open connections without limit.
    :returns: The server; call ``run()`` to serve until shutdown.
    """
    import asyncio

    import uvicorn

    class _ShutdownSignalingServer(uvicorn.server.Server):
        """uvicorn.Server that signals active SSE subscribers before the
        graceful-shutdown wait starts.

        uvicorn calls ``Server.shutdown()`` in this order:
          1. close listening sockets / call connection.shutdown()
          2. ``asyncio.wait_for(_wait_tasks_to_complete(), timeout=…)``
          3. force-cancel remaining tasks on timeout
          4. run the ASGI lifespan shutdown handler

        The ASGI lifespan ``finally`` block runs at step 4 — too late. SSE
        generators waiting on a heartbeat tick are already force-cancelled by
        step 3, which produces spurious ``CancelledError`` tracebacks.
        Overriding here lets us drain SSE streams before step 2 so they exit
        cleanly within the graceful window.
        """

        async def shutdown(self, sockets: list[socket.socket] | None = None) -> None:
            from omnigent.runtime import session_stream
            from omnigent.server import shutdown_state

            # The runner tunnels close next; their disconnect handlers must
            # read that loss as ours, not as the runners dying.
            shutdown_state.mark_server_shutting_down()
            session_stream.shutdown_all()
            # Yield so streams consume _DONE and exit before transports close.
            # No [DONE] reaches browsers: they must reconnect after restart.
            # Without this pause the generators write to an already-closing
            # transport, leaving connections open past the graceful window.
            await asyncio.sleep(0)
            await super().shutdown(sockets)

    return _ShutdownSignalingServer(config)
