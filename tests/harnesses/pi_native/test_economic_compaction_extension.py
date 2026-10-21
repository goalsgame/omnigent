"""Exercise the generated bridge's compaction hook without billable inference."""

import shutil
import subprocess
from pathlib import Path

import pytest


def test_economic_summary_hook_preserves_budgets_usage_and_abort_signal(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node required")
    extension = (
        Path(__file__).resolve().parents[3]
        / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"
    )
    script = Path(__file__).with_name("economic-compaction.cjs").read_text()
    result = subprocess.run(
        [node, "-e", script, str(extension), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
