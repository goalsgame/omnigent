"""Exercise native discovery through both the bridge and the installed Pi SDK."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def test_bridge_discovery_registration_and_legacy_fallback() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node is required for the native bridge")
    result = subprocess.run(
        [node, str(ROOT / "omnigent/resources/pi_native/omnigent_pi_native_extension.test.js")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_native_codemode_sdk_preserves_authorization() -> None:
    node, pi = shutil.which("node"), shutil.which("pi")
    if node is None or pi is None:
        pytest.skip("Native SDK integration requires Node and Pi")
    package = None
    for parent in Path(pi).resolve().parents:
        manifest = parent / "package.json"
        if manifest.is_file():
            data = json.loads(manifest.read_text())
            if data.get("name") == "@earendil-works/pi-coding-agent":
                package = parent
                break
    if package is None:
        pytest.skip("Native SDK integration requires the npm Pi distribution")
    version = json.loads((package / "package.json").read_text())["version"]
    if tuple(int(part) for part in version.split(".")[:2]) < (1, 3):
        pytest.skip("Native discovery integration requires Pi 1.3 or newer")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("verify_codemode.mjs")),
            str(package),
            str(ROOT / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"),
            version,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
