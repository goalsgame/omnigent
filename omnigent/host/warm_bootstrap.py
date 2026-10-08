"""Bind an already-running sandbox Pod to one managed host.

The preparation container owns a private activation file in a memory-backed
volume. The host container preloads the runner graph without an identity, then
starts the normal host in the same interpreter after workspace preparation
succeeds. Credentials never enter command argv or bootstrap status output.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from types import FrameType
from typing import Literal

from omnigent.host.identity_env import (
    HOST_ID_ENV_VAR,
    HOST_NAME_ENV_VAR,
    HOST_TOKEN_ENV_VAR,
)
from omnigent.host.workspace_errors import (
    GITHUB_CHECKOUT_UNCONNECTED,
    GITHUB_CHECKOUT_UNCONNECTED_EXIT,
    WORKSPACE_ERROR_MESSAGES,
)
from omnigent.startup_timing import startup_span, startup_stderr_logging, startup_timing_enabled

ACTIVATION_DIR_ENV_VAR = "OMNIGENT_ACTIVATION_DIR"
POD_UID_ENV_VAR = "OMNIGENT_POD_UID"
_DEFAULT_ACTIVATION_DIR = "/run/omnigent-activation"
_MAX_ACTIVATION_BYTES = 1024 * 1024
_POLL_INTERVAL = 0.2
_GENERATION_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}\Z")
_STAGES = frozenset({"preparing", "prepared", "failed"})


class BootstrapError(Exception):
    """A bootstrap failure whose message is safe to expose to the caller."""


def _pod_uid() -> str:
    value = os.environ.get(POD_UID_ENV_VAR)
    if not value:
        raise BootstrapError("The sandbox Pod UID is missing.")
    return value


def _string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value or "\0" in value:
        raise BootstrapError("Invalid activation payload.")
    return value


@dataclass(frozen=True, repr=False)
class Activation:
    """Private, immutable assignment for one Pod lifetime."""

    version: int
    pod_uid: str
    host_id: str
    host_name: str
    token: str
    server_url: str
    prepare_command: tuple[str, ...]
    generation: str

    @classmethod
    def parse(cls, value: object) -> Activation:
        if not isinstance(value, dict):
            raise BootstrapError("Invalid activation payload.")
        if _string(value, "pod_uid") != _pod_uid():
            raise BootstrapError("Activation targets a different Pod.")
        if set(value) != set(cls.__dataclass_fields__):
            raise BootstrapError("Invalid activation payload.")
        if type(value.get("version")) is not int or value["version"] != 1:
            raise BootstrapError("Unsupported activation version.")
        command = value.get("prepare_command")
        if (
            not isinstance(command, list)
            or not command
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
            or not command[0]
        ):
            raise BootstrapError("Invalid preparation command.")
        generation = _string(value, "generation")
        if not _GENERATION_RE.fullmatch(generation):
            raise BootstrapError("Invalid activation generation.")
        return cls(
            version=1,
            pod_uid=_string(value, "pod_uid"),
            host_id=_string(value, "host_id"),
            host_name=_string(value, "host_name"),
            token=_string(value, "token"),
            server_url=_string(value, "server_url"),
            prepare_command=tuple(command),
            generation=generation,
        )

    def environment(self) -> dict[str, str]:
        if self.pod_uid != _pod_uid():
            raise BootstrapError("Activation targets a different Pod.")
        return {
            **os.environ,
            HOST_ID_ENV_VAR: self.host_id,
            HOST_NAME_ENV_VAR: self.host_name,
            HOST_TOKEN_ENV_VAR: self.token,
        }


def _state_dir(*, create: bool = False) -> Path:
    directory = Path(os.environ.get(ACTIVATION_DIR_ENV_VAR, _DEFAULT_ACTIVATION_DIR))
    # The fsGroup-writable emptyDir root is owned by root, so create our own dir.
    directory /= "private"
    if create:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.chmod(0o700)
    return directory


def _write_json(path: Path, value: object) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary)


def _read_json(path: Path) -> object | None:
    try:
        with path.open() as stream:
            raw = stream.read(_MAX_ACTIVATION_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw.encode("utf-8")) > _MAX_ACTIVATION_BYTES:
        raise BootstrapError("Invalid bootstrap state.")
    try:
        return json.loads(raw)
    except (ValueError, UnicodeError):
        raise BootstrapError("Invalid bootstrap state.") from None


def _load_activation(directory: Path) -> Activation | None:
    value = _read_json(directory / "activation.json")
    return Activation.parse(value) if value is not None else None


@contextlib.contextmanager
def _lock(path: Path, *, nonblocking: bool = False) -> Iterator[None]:
    import fcntl

    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if nonblocking else 0)
        try:
            fcntl.flock(descriptor, flags)
        except BlockingIOError:
            raise BootstrapError("Workspace preparation is already running.") from None
        yield
    finally:
        os.close(descriptor)


def activate(value: object) -> None:
    """Atomically accept one assignment, allowing identical delivery retries."""
    activation = Activation.parse(value)
    directory = _state_dir(create=True)
    with _lock(directory / "activation.lock"):
        existing = _load_activation(directory)
        if existing is not None:
            if existing != activation:
                raise BootstrapError("This Pod is already bound to another activation.")
            return
        _write_json(directory / "activation.json", asdict(activation))


def status() -> dict[str, str | None]:
    """Return only nonsecret preparation metadata."""
    directory = _state_dir()
    activation = _load_activation(directory)
    if activation is None:
        return {"stage": "waiting", "generation": None}
    value = _read_json(directory / "status.json")
    stage = "bound"
    if value is not None:
        if (
            not isinstance(value, dict)
            or value.get("pod_uid") != activation.pod_uid
            or value.get("generation") != activation.generation
            or not isinstance(value.get("stage"), str)
            or value.get("stage") not in _STAGES
        ):
            raise BootstrapError("Invalid bootstrap status.")
        stage = value["stage"]
    result: dict[str, str | None] = {"stage": stage, "generation": activation.generation}
    if stage == "failed" and isinstance(value, dict):
        error_code = value.get("error_code")
        if isinstance(error_code, str) and error_code in WORKSPACE_ERROR_MESSAGES:
            result["error_code"] = error_code
    return result


def _set_stage(
    directory: Path,
    activation: Activation,
    stage: Literal["preparing", "prepared", "failed"],
    error_code: str | None = None,
) -> None:
    _write_json(
        directory / "status.json",
        {
            "pod_uid": activation.pod_uid,
            "generation": activation.generation,
            "stage": stage,
            "error_code": error_code,
        },
    )


class _Signals:
    def __init__(self) -> None:
        self.signum: int | None = None
        self.child: subprocess.Popen[bytes] | None = None

    def forward(self, signum: int, _frame: FrameType | None) -> None:
        self.signum = signum
        if self.child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.child.pid, signum)


@contextlib.contextmanager
def _signals() -> Iterator[_Signals]:
    state = _Signals()
    previous = {sig: signal.signal(sig, state.forward) for sig in (signal.SIGTERM, signal.SIGINT)}
    try:
        yield state
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def _prepare_once(directory: Path, activation: Activation, signals: _Signals) -> None:
    _set_stage(directory, activation, "preparing")
    try:
        with startup_span("bootstrap.prepare_spawn", host_id=activation.host_id):
            signals.child = subprocess.Popen(
                activation.prepare_command,
                env=activation.environment(),
                start_new_session=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if signals.signum is not None:
            signals.forward(signals.signum, None)
        with startup_span("bootstrap.prepare_wait", host_id=activation.host_id):
            returncode = signals.child.wait()
    except (OSError, subprocess.SubprocessError):
        returncode = 1
    finally:
        signals.child = None
    if signals.signum is None:
        _set_stage(
            directory,
            activation,
            "prepared" if returncode == 0 else "failed",
            GITHUB_CHECKOUT_UNCONNECTED
            if returncode == GITHUB_CHECKOUT_UNCONNECTED_EXIT
            else None,
        )


def prepare() -> int:
    """Wait for assignment, prepare once, then stay alive for status requests."""
    if startup_timing_enabled():
        logging.basicConfig(level=logging.INFO)
    directory = _state_dir(create=True)
    with _signals() as signals, _lock(directory / "prepare.lock", nonblocking=True):
        _write_json(directory / "ready.json", {"pod_uid": _pod_uid()})
        while signals.signum is None:
            activation = _load_activation(directory)
            if (
                activation is not None
                and signals.signum is None
                and status()["stage"] in {"bound", "preparing"}
            ):
                _prepare_once(directory, activation, signals)
            with contextlib.suppress(ChildProcessError):
                while os.waitpid(-1, os.WNOHANG)[0]:
                    pass
            if signals.signum is None:
                time.sleep(_POLL_INTERVAL)
    return 128 + signals.signum if signals.signum is not None else 0


def _runtime_ready_path() -> Path:
    return Path(tempfile.gettempdir()) / "omnigent-warm-runtime.json"


def _preload_host_runtime() -> None:
    """Import host code and cache binary versions without reading owner credentials."""
    import omnigent.host.connect  # noqa: F401
    from omnigent.onboarding.harness_install import preload_harness_cli_versions

    preload_harness_cli_versions()


def host() -> int:
    """Preload without an identity, then reuse the runtime after preparation."""
    marker = _runtime_ready_path()
    marker.unlink(missing_ok=True)
    zygote = None
    try:
        with _signals() as signals:
            import click

            from omnigent._platform import IS_POSIX
            from omnigent.cli import cli
            from omnigent.host.runner_zygote import ZygoteManager, ZygoteUnavailable
            from omnigent.process_logging import env_truthy

            _preload_host_runtime()
            optout = os.environ.get("OMNIGENT_RUNNER_ZYGOTE")
            if IS_POSIX and (optout is None or env_truthy(optout)):
                zygote = ZygoteManager(preload_metadata=True)
                try:
                    zygote.start()
                except ZygoteUnavailable:
                    raise BootstrapError("Warm runner preload failed.") from None
            if signals.signum is not None:
                return 128 + signals.signum
            _write_json(
                marker,
                {
                    "pod_uid": _pod_uid(),
                    "host_pid": os.getpid(),
                    "zygote_pid": zygote.pid if zygote else None,
                },
            )
            while signals.signum is None:
                if zygote is not None and not zygote.is_running():
                    raise BootstrapError("Warm runner preload stopped.")
                activation = _load_activation(_state_dir())
                if activation is not None:
                    preparation = status()
                    stage = preparation["stage"]
                    if stage == "failed":
                        raise BootstrapError(
                            WORKSPACE_ERROR_MESSAGES.get(
                                preparation.get("error_code") or "",
                                "Sandbox workspace preparation failed.",
                            )
                        )
                    if stage == "prepared":
                        os.environ.update(activation.environment())
                        break
                time.sleep(_POLL_INTERVAL)
            else:
                return 128 + signals.signum if signals.signum is not None else 0
        # Restore handlers before the ordinary host installs its own.
        try:
            with startup_stderr_logging():
                with startup_span("bootstrap.host_handoff", host_id=activation.host_id):
                    pass
                cli.main(
                    ["host", "--server", activation.server_url, "--no-open", "--non-interactive"],
                    prog_name="omnigent",
                    obj={"warm_runner_zygote": zygote},
                    standalone_mode=False,
                )
        except click.ClickException as exc:
            exc.show()
            return exc.exit_code
        except click.Abort:
            click.echo("Aborted!", err=True)
            return 1
        return 0
    finally:
        marker.unlink(missing_ok=True)
        if zygote is not None:
            zygote.stop()


def runtime_ready() -> bool:
    """Require this container's preloaded process in addition to preparation readiness."""
    value = _read_json(_runtime_ready_path())
    if not isinstance(value, dict) or value.get("pod_uid") != _pod_uid():
        return False
    if not ready():
        return False
    # Once prepared, the ordinary host owns forkserver recovery and fallback.
    keys = ("host_pid",) if status()["stage"] == "prepared" else ("host_pid", "zygote_pid")
    for key in keys:
        pid = value.get(key)
        if pid is None and key == "zygote_pid":
            continue
        if type(pid) is not int or pid <= 0:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
    return True


def ready() -> bool:
    value = _read_json(_state_dir() / "ready.json")
    return (
        isinstance(value, dict)
        and value.get("pod_uid") == _pod_uid()
        and status()["stage"] != "failed"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("prepare", "host", "activate", "status", "ready"))
    parser.add_argument("--runtime", action="store_true", help="Require host runtime preload.")
    args = parser.parse_args(argv)
    try:
        if args.mode == "activate":
            raw = sys.stdin.buffer.readline(_MAX_ACTIVATION_BYTES + 1)
            if len(raw) > _MAX_ACTIVATION_BYTES:
                raise BootstrapError("Activation payload is too large.")
            try:
                value = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BootstrapError("Invalid activation payload.") from None
            activate(value)
            return 0
        if args.mode == "status":
            print(json.dumps(status(), separators=(",", ":")))
            return 0
        if args.mode == "ready":
            return 0 if (runtime_ready() if args.runtime else ready()) else 1
        if args.mode == "prepare":
            return prepare()
        return host()
    except BootstrapError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("Warm sandbox bootstrap failed.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
