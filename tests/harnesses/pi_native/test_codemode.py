"""Exercise native discovery through both the bridge and the installed Pi SDK."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]

#: First Pi release whose ``searchTools()`` ranks a tool named by the query first.
NAME_RANKING_MIN_VERSION = (1, 5)


def _minor_version(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("-")[0].split(".")[:2])


def _verify_codemode_flags(version: str) -> list[str]:
    """Extra ``verify_codemode.mjs`` arguments for the installed Pi ``version``."""
    if _minor_version(version) < NAME_RANKING_MIN_VERSION:
        return ["skip-name-ranking"]
    return []


@pytest.mark.parametrize(
    ("version", "flags"),
    [
        ("1.3.0", ["skip-name-ranking"]),
        ("1.4.9", ["skip-name-ranking"]),
        ("1.5.0-beta.1", []),
        ("1.5.2", []),
        ("2.0.0", []),
    ],
)
def test_name_ranking_checks_follow_pi_version(version: str, flags: list[str]) -> None:
    assert _verify_codemode_flags(version) == flags


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


def _installed_pi_sdk() -> tuple[str, Path, str]:
    """Return ``(node, package_root, version)`` for an installed Pi 1.3+ npm package."""
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
    if _minor_version(version) < (1, 3):
        pytest.skip("Native discovery integration requires Pi 1.3 or newer")
    return node, package, version


def test_native_codemode_sdk_preserves_authorization() -> None:
    node, package, version = _installed_pi_sdk()
    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("verify_codemode.mjs")),
            str(package),
            str(ROOT / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"),
            version,
            *_verify_codemode_flags(version),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("startup", ["listed", "empty"])
@pytest.mark.parametrize("exposure", ["direct", "codemode"])
def test_native_session_follows_added_and_removed_mcp_tools(exposure: str, startup: str) -> None:
    node, package, _version = _installed_pi_sdk()
    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("verify_mcp_refresh.mjs")),
            str(package),
            str(ROOT / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"),
            exposure,
            startup,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"MCP refresh verified ({exposure}, {startup} startup)" in result.stdout
