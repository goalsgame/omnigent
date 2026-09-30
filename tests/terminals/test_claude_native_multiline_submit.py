"""Bounded tmux/PTY regression for Claude-native multiline submission.

The child process in this test is a small synthetic Claude-shaped terminal. Its
``FAULT_INJECTED_BLANK_PROMPT_ROW`` state deliberately places a multiline draft
below a blank ``❯`` row after the first Enter. That is a deterministic terminal
application fault injection, not a reproduction of Claude Code itself.

No server, credentials, model, or network are involved. The only process and
tmux server created by the test live under its ``tmp_path`` and private socket.
"""

from __future__ import annotations

import json
import shlex
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from omnigent.harnesses.claude_native import bridge as claude_native_bridge

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="Claude-native tmux/PTY regression needs `tmux` on PATH",
)


_MESSAGE = "TMUX_MULTILINE\nsecond line stays in the same message"
_PANE_TARGET = "claude-native-regression:0.0"
_SESSION_NAME = "claude-native-regression"


_FAULT_INJECTED_TERMINAL_SOURCE = r'''#!/usr/bin/env python3
"""Synthetic Claude-shaped terminal used by one bounded bridge regression."""

import json
import os
import sys
import termios
import tty


LOG_PATH = sys.argv[1]
RULE = "─" * 32
PROMPT = "❯"
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
CLEAR_KEYS = (b"\x1b[97;5u", b"\x1b[107;5u")
FAULT_INJECTED_BLANK_PROMPT_ROW = "fault_injected_blank_prompt_row"


class SyntheticClaudeTerminal:
    def __init__(self):
        self.draft = ""
        self.paste_count = 0
        self.enter_count = 0
        self.submit_count = 0
        self.fault_injected = False

    def log(self, event, **fields):
        record = {
            "event": event,
            "paste_count": self.paste_count,
            "enter_count": self.enter_count,
            "submit_count": self.submit_count,
            **fields,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            json.dump(record, handle, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def render(self):
        lines = ["synthetic Claude-shaped terminal", RULE]
        draft_lines = self.draft.rstrip("\n").split("\n") if self.draft else []
        if self.fault_injected:
            # Deliberate fault injection: the prompt is blank and the draft is
            # rendered on continuation rows inside the same composer frame.
            lines.append(PROMPT)
            lines.extend(draft_lines)
        else:
            lines.append(PROMPT + (" " + draft_lines[0] if draft_lines else ""))
            lines.extend(draft_lines[1:])
        lines.extend([RULE, "synthetic footer"])
        sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines) + "\r\n")
        sys.stdout.flush()

    def receive_paste(self, payload):
        self.paste_count += 1
        self.draft += payload
        self.render()
        self.log("paste", received_payload=payload)

    def receive_enter(self):
        self.enter_count += 1
        if self.enter_count == 1:
            self.fault_injected = True
            self.render()
            self.log(
                FAULT_INJECTED_BLANK_PROMPT_ROW,
                received_payload=self.draft,
            )
            return

        self.submit_count += 1
        self.log(
            "submit",
            received_payload=self.draft.removesuffix("\n"),
        )
        self.draft = ""
        self.fault_injected = False
        self.render()

    def run(self):
        original_attrs = termios.tcgetattr(0)
        tty.setraw(0)
        try:
            sys.stdout.write("\x1b[?2004h")  # Ask tmux to bracket paste-buffer delivery.
            sys.stdout.flush()
            self.render()
            buffer = b""
            in_paste = False
            paste_buffer = bytearray()
            while True:
                chunk = os.read(0, 4096)
                if not chunk:
                    return
                buffer += chunk
                while buffer:
                    if in_paste:
                        end = buffer.find(PASTE_END)
                        if end == -1:
                            keep = len(PASTE_END) - 1
                            if len(buffer) > keep:
                                paste_buffer.extend(buffer[:-keep])
                                buffer = buffer[-keep:]
                            break
                        paste_buffer.extend(buffer[:end])
                        buffer = buffer[end + len(PASTE_END) :]
                        in_paste = False
                        self.receive_paste(
                            bytes(paste_buffer).decode("utf-8", "replace").replace("\r", "\n")
                        )
                        paste_buffer.clear()
                        continue

                    if len(buffer) < len(PASTE_START) and PASTE_START.startswith(buffer):
                        break
                    if buffer.startswith(PASTE_START):
                        in_paste = True
                        buffer = buffer[len(PASTE_START) :]
                        continue

                    if any(
                        len(buffer) < len(key) and key.startswith(buffer) for key in CLEAR_KEYS
                    ):
                        break
                    clear_key = next((key for key in CLEAR_KEYS if buffer.startswith(key)), None)
                    if clear_key is not None:
                        buffer = buffer[len(clear_key) :]
                        self.draft = ""
                        self.fault_injected = False
                        self.render()
                        continue

                    byte = buffer[0]
                    buffer = buffer[1:]
                    if byte in (0x01, 0x0B):  # C-a / C-k: clear the composer.
                        self.draft = ""
                        self.fault_injected = False
                        self.render()
                    elif byte in (0x0A, 0x0D):
                        self.receive_enter()
                    elif byte >= 0x20:
                        self.draft += chr(byte)
                        self.render()
        finally:
            termios.tcsetattr(0, termios.TCSADRAIN, original_attrs)


if __name__ == "__main__":
    SyntheticClaudeTerminal().run()
'''


def _read_log(path: Path) -> list[dict[str, Any]]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[dict[str, Any]] = []
    for line in lines:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def _start_synthetic_terminal(
    tmp_path: Path,
    *,
    socket_path: Path,
    log_path: Path,
) -> None:
    source_path = tmp_path / "fault_injected_claude_terminal.py"
    source_path.write_text(textwrap.dedent(_FAULT_INJECTED_TERMINAL_SOURCE), encoding="utf-8")
    source_path.chmod(0o700)
    command = shlex.join([sys.executable, str(source_path), str(log_path)])
    subprocess.run(
        [
            "tmux",
            "-f",
            "/dev/null",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            _SESSION_NAME,
            command,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=2.0,
    )


def _stop_synthetic_terminal(socket_path: Path) -> None:
    """Kill only the private tmux server created by this test."""
    for command in ("kill-session", "kill-server"):
        subprocess.run(
            ["tmux", "-S", str(socket_path), command, "-t", _SESSION_NAME]
            if command == "kill-session"
            else ["tmux", "-S", str(socket_path), command],
            check=False,
            capture_output=True,
            text=True,
            timeout=2.0,
        )


def test_multiline_submit_recovers_from_fault_injected_blank_prompt_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A continuation-row draft is submitted after the injected first Enter."""
    monkeypatch.setattr(claude_native_bridge, "_TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr(claude_native_bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setattr(claude_native_bridge, "_TMUX_READY_SLOW_BOOT_TIMEOUT_S", 3.0)
    monkeypatch.setattr(claude_native_bridge, "_TMUX_SEND_TIMEOUT_S", 1.0)
    monkeypatch.setattr(claude_native_bridge, "_PASTE_COMMIT_TIMEOUT_S", 2.0)
    monkeypatch.setattr(claude_native_bridge, "_SUBMIT_VERIFY_TIMEOUT_S", 3.0)
    monkeypatch.setattr(claude_native_bridge, "_SUBMIT_RETRY_INTERVAL_S", 0.2)
    monkeypatch.setattr(claude_native_bridge, "_SUBMIT_RETRY_MAX_INTERVAL_S", 0.2)
    monkeypatch.setattr(claude_native_bridge, "_CLAUDE_READY_POLL_INTERVAL_S", 0.05)
    monkeypatch.setattr(claude_native_bridge, "_PASTE_SETTLE_S", 0.0)

    bridge_dir = tmp_path / "bridges" / "claude-native-regression"
    socket_path = tmp_path / "tmux.sock"
    log_path = tmp_path / "terminal.jsonl"
    try:
        _start_synthetic_terminal(tmp_path, socket_path=socket_path, log_path=log_path)
        claude_native_bridge.write_tmux_target(
            bridge_dir,
            socket_path=socket_path,
            tmux_target=_PANE_TARGET,
        )
        claude_native_bridge.inject_user_message(
            bridge_dir,
            content=_MESSAGE,
            timeout_s=2.0,
        )

        records = _read_log(log_path)
        fault_records = [
            record
            for record in records
            if record.get("event") == "fault_injected_blank_prompt_row"
        ]
        paste_records = [record for record in records if record.get("event") == "paste"]
        submit_records = [record for record in records if record.get("event") == "submit"]
        assert len(fault_records) == 1, records
        assert len(paste_records) == 1, records
        assert len(submit_records) == 1, records
        assert [record["event"] for record in records] == [
            "paste",
            "fault_injected_blank_prompt_row",
            "submit",
        ]
        assert paste_records[0]["received_payload"] == _MESSAGE + "\n"
        assert fault_records[0]["received_payload"] == _MESSAGE + "\n"
        assert submit_records[0]["received_payload"] == _MESSAGE
        assert submit_records[0]["paste_count"] == 1
        assert submit_records[0]["enter_count"] == 2
        assert submit_records[0]["submit_count"] == 1

        final_pane = claude_native_bridge._capture_pane(str(socket_path), _PANE_TARGET)
        assert [line.strip() for line in final_pane.splitlines()].count("❯") == 1, final_pane
        assert _MESSAGE.splitlines()[0] not in final_pane, final_pane
    finally:
        _stop_synthetic_terminal(socket_path)
