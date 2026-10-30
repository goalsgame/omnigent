"""Remote microsandbox launcher: micro-VMs on a dedicated host behind an HTTP API.

Implements the managed-launch subset of
:class:`~omnigent.onboarding.sandboxes.base.SandboxLauncher` against a sandbox
host that runs `microsandbox <https://github.com/superradcompany/microsandbox>`_
and exposes it over HTTP. The server never imports the microsandbox SDK; it only
speaks to the host API:

- ``PUT /v1/sandboxes/{name}`` creates (idempotently) a detached micro-VM.
- ``POST /v1/sandboxes/{name}/exec`` runs a shell command and returns its output.
- ``POST /v1/sandboxes/{name}/start`` / ``stop`` / ``touch``, ``GET`` and
  ``DELETE`` manage its lifecycle.

Each request carries a bearer token: a Google-signed ID token for the configured
audience when ``audience`` is set (the server's workload identity), otherwise the
value of the environment variable named by ``token_env``.

Stopped (idle-drained) sandboxes keep their filesystem and an optional per-sandbox
workspace volume, so ``can_resume`` is ``True`` and the managed wake path restarts
them in place.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import threading
import time
from collections.abc import Sequence
from typing import Any, ClassVar

import click
import httpx

from omnigent.onboarding.sandboxes.base import (
    DEFAULT_HOST_IMAGE,
    ExecModelHostLauncher,
    RemoteCommandResult,
    SandboxGoneError,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities

DEFAULT_TOKEN_ENV: str = "OMNIGENT_MICROSANDBOX_REMOTE_TOKEN"
"""Server environment variable holding a static bearer token for the host API."""

_DEFAULT_CPUS: int = 2
_DEFAULT_MEMORY_MIB: int = 4096
_DEFAULT_IDLE_TIMEOUT_S: int = 24 * 3600

_REQUEST_TIMEOUT_S: float = 60.0
_CREATE_TIMEOUT_S: float = 900.0
_RUN_TIMEOUT_S: float = 600.0
_ID_TOKEN_REFRESH_S: float = 45 * 60
_CREATE_ATTEMPTS: int = 3

_NAME_INVALID = re.compile(r"[^a-z0-9-]+")


def sandbox_name(label: str) -> str:
    """
    Turn a launch label into a unique host-API sandbox name.

    :param label: Human-readable label, e.g. ``"managed-a1b2c3d4"``.
    :returns: A lowercase name of at most 63 characters, unique per call.
    """
    base = _NAME_INVALID.sub("-", label.lower()).strip("-")[:50].strip("-") or "sandbox"
    return f"{base}-{secrets.token_hex(4)}"


class _Credentials:
    """Bearer token source shared by every launcher for one endpoint."""

    def __init__(self, audience: str | None, token_env: str) -> None:
        self._audience = audience
        self._token_env = token_env
        self._lock = threading.Lock()
        self._token: str | None = None
        self._fetched_at = 0.0

    def token(self) -> str:
        if not self._audience:
            value = os.environ.get(self._token_env, "")
            if not value:
                raise click.ClickException(
                    f"remote microsandbox token is not set; export {self._token_env} "
                    "or configure sandbox.microsandbox_remote.audience"
                )
            return value
        with self._lock:
            if self._token is None or time.monotonic() - self._fetched_at > _ID_TOKEN_REFRESH_S:
                try:
                    from google.auth.transport.requests import Request
                    from google.oauth2 import id_token
                except ImportError as exc:
                    raise click.ClickException(
                        "an audience requires google-auth; install 'omnigent[vertex]'"
                    ) from exc
                try:
                    token = id_token.fetch_id_token(Request(), self._audience)
                except Exception as exc:
                    raise click.ClickException(
                        f"could not obtain an ID token for the host API: {exc}"
                    ) from exc
                if not token:
                    raise click.ClickException("could not obtain an ID token for the host API")
                self._token = token
                self._fetched_at = time.monotonic()
                return token
            return self._token


_credentials: dict[tuple[str | None, str], _Credentials] = {}
_credentials_lock = threading.Lock()


def _shared_credentials(audience: str | None, token_env: str) -> _Credentials:
    with _credentials_lock:
        return _credentials.setdefault((audience, token_env), _Credentials(audience, token_env))


class RemoteMicrosandboxLauncher(ExecModelHostLauncher):
    """Managed-only launcher for micro-VMs on a remote microsandbox host."""

    provider: ClassVar[str] = "microsandbox_remote"
    supports_cli_bootstrap: ClassVar[bool] = False
    can_resume: ClassVar[bool] = True

    def __init__(
        self,
        *,
        endpoint: str,
        audience: str | None = None,
        token_env: str | None = None,
        image: str | None = None,
        cpus: int | None = None,
        memory_mib: int | None = None,
        max_memory_mib: int | None = None,
        idle_timeout_s: int | None = None,
        env: Sequence[str] | None = None,
        workspace_path: str | None = None,
        workspace_quota_mib: int | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """
        Initialize the launcher.

        :param endpoint: Base URL of the host API, e.g. ``"http://10.0.34.44:8740"``.
        :param audience: ID-token audience the host verifies; ``None`` uses the
            static token from *token_env*.
        :param token_env: Server environment variable holding a static token;
            ``None`` uses :data:`DEFAULT_TOKEN_ENV`.
        :param image: Host image reference; ``None`` uses the default host image.
        :param cpus: vCPUs per sandbox; ``None`` uses 2. Idle vCPUs cost the host
            nothing, so this can be close to the host's core count.
        :param memory_mib: Starting guest memory in MiB; ``None`` uses 4096.
        :param max_memory_mib: Memory ceiling for live growth; ``None`` uses
            *memory_mib*.
        :param idle_timeout_s: Seconds of inactivity before the host stops the
            sandbox (restartable via :meth:`resume`); ``None`` uses 24h, ``0``
            disables it.
        :param env: Names of server environment variables injected into every
            sandbox, e.g. ``["GIT_TOKEN"]``.
        :param workspace_path: Guest path of a per-sandbox workspace volume that
            survives stop/start, e.g. ``"/home/omnigent"``; ``None`` keeps the
            workspace on the root filesystem.
        :param workspace_quota_mib: Quota of the workspace volume in MiB.
        :param transport: Optional HTTP transport, for tests.
        """
        self._endpoint = endpoint.rstrip("/")
        self._credentials = _shared_credentials(audience, token_env or DEFAULT_TOKEN_ENV)
        self._image = image
        self._cpus = cpus or _DEFAULT_CPUS
        self._memory_mib = memory_mib or _DEFAULT_MEMORY_MIB
        self._max_memory_mib = max(max_memory_mib or self._memory_mib, self._memory_mib)
        self._idle_timeout_s = (
            idle_timeout_s if idle_timeout_s is not None else _DEFAULT_IDLE_TIMEOUT_S
        )
        self._env_names = tuple(env or ())
        self._workspace_path = workspace_path
        self._workspace_quota_mib = workspace_quota_mib
        self._transport = transport

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=False,
            managed_launch=True,
            local_port_forward=False,
            resume_stopped=True,
            programmatic_terminate=True,
            file_copy=False,
            streaming_exec=False,
            foreground_exec=False,
            git_clone_options=super().capabilities.git_clone_options,
        )

    def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        timeout: float = _REQUEST_TIMEOUT_S,
    ) -> httpx.Response:
        headers = {"Authorization": f"Bearer {self._credentials.token()}"}
        try:
            with httpx.Client(transport=self._transport, timeout=timeout) as client:
                return client.request(method, self._endpoint + path, json=json, headers=headers)
        except httpx.HTTPError as exc:
            raise click.ClickException(f"microsandbox host request failed: {exc}") from exc

    @staticmethod
    def _check(response: httpx.Response, action: str) -> httpx.Response:
        if response.is_success:
            return response
        try:
            detail = response.json().get("detail", response.text)
        except ValueError:
            detail = response.text
        raise click.ClickException(
            f"microsandbox host could not {action}: HTTP {response.status_code} {detail}"
        )

    def _sandbox_env(self) -> dict[str, str]:
        return {name: os.environ[name] for name in self._env_names if name in os.environ}

    def prepare(self) -> None:
        self._check(self._request("GET", "/v1/host"), "report its capacity")

    def provision(self, name: str) -> str:
        sandbox_id = sandbox_name(name)
        body: dict[str, Any] = {
            "image": self._image or DEFAULT_HOST_IMAGE,
            "cpus": self._cpus,
            "memory_mib": self._memory_mib,
            "max_memory_mib": self._max_memory_mib,
            "idle_timeout_s": self._idle_timeout_s,
            "env": self._sandbox_env(),
        }
        if self._workspace_path:
            body["workspace_path"] = self._workspace_path
            body["workspace_quota_mib"] = self._workspace_quota_mib
        click.echo(f"▸ Creating remote microsandbox '{sandbox_id}'")
        failure: click.ClickException | None = None
        for _attempt in range(_CREATE_ATTEMPTS):
            try:
                response = self._request(
                    "PUT", f"/v1/sandboxes/{sandbox_id}", json=body, timeout=_CREATE_TIMEOUT_S
                )
            except click.ClickException as exc:
                failure = exc
                continue
            if response.is_success:
                return sandbox_id
            try:
                self._check(response, f"create sandbox '{sandbox_id}'")
            except click.ClickException as exc:
                failure = exc
            if response.status_code < 500:
                break
        # The host may have created the VM before the response was lost.
        with contextlib.suppress(click.ClickException):
            self._request("DELETE", f"/v1/sandboxes/{sandbox_id}", timeout=_CREATE_TIMEOUT_S)
        raise failure or click.ClickException(f"could not create sandbox '{sandbox_id}'")

    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        response = self._request(
            "POST",
            f"/v1/sandboxes/{sandbox_id}/exec",
            json={"command": command, "timeout_s": _RUN_TIMEOUT_S},
            timeout=_RUN_TIMEOUT_S + _REQUEST_TIMEOUT_S,
        )
        if response.status_code == 404:
            raise SandboxGoneError(f"remote microsandbox '{sandbox_id}' no longer exists")
        body = self._check(response, f"run a command in '{sandbox_id}'").json()
        exit_code = body["exit_code"]
        result = RemoteCommandResult(
            returncode=-1 if exit_code is None else int(exit_code),
            stdout=body["stdout"],
            stderr=body["stderr"],
        )
        if check and result.returncode != 0:
            raise click.ClickException(
                f"command failed in '{sandbox_id}' (exit {result.returncode}): "
                f"{result.stderr.strip() or result.stdout.strip()}"
            )
        return result

    def keep_alive(self, sandbox_id: str) -> bool | None:
        try:
            response = self._request("POST", f"/v1/sandboxes/{sandbox_id}/touch")
        except click.ClickException as exc:
            click.echo(f"  → could not refresh idle timer for '{sandbox_id}': {exc}", err=True)
            return False
        if response.status_code != 204:
            click.echo(
                f"  → could not refresh idle timer for '{sandbox_id}': "
                f"HTTP {response.status_code}",
                err=True,
            )
            return False
        return True

    def resume(self, sandbox_id: str) -> None:
        response = self._request(
            "POST", f"/v1/sandboxes/{sandbox_id}/start", timeout=_CREATE_TIMEOUT_S
        )
        if response.status_code == 404:
            raise SandboxGoneError(f"remote microsandbox '{sandbox_id}' no longer exists")
        self._check(response, f"start sandbox '{sandbox_id}'")

    def is_running(self, sandbox_id: str) -> bool | None:
        try:
            response = self._request("GET", f"/v1/sandboxes/{sandbox_id}")
        except click.ClickException:
            return None
        if response.status_code == 404:
            return False
        if not response.is_success:
            return None
        return response.json().get("status") == "running"

    def terminate(self, sandbox_id: str) -> None:
        self._check(
            self._request("DELETE", f"/v1/sandboxes/{sandbox_id}", timeout=_CREATE_TIMEOUT_S),
            f"remove sandbox '{sandbox_id}'",
        )
