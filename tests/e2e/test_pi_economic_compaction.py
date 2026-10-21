"""Own-environment native Pi compaction/continuation with a local synthetic model."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from omnigent.inner.pi_compaction import POLICY_FILE, economic_settings


def test_native_pi_compacts_then_continues_without_enlarging_summary_budget(tmp_path):
    node, pi = shutil.which("node"), shutil.which("pi")
    if not node or not pi:
        pytest.skip("Installed native Pi and Node required")
    settings, policy = economic_settings(
        {}, {"managed/vendor/large-model": 1050000}, '{"vendor/large-model":200000}'
    )
    (tmp_path / "settings.json").write_text(json.dumps(settings))
    (tmp_path / POLICY_FILE).write_text(json.dumps(policy))
    extension = (
        Path(__file__).resolve().parents[2]
        / "omnigent/resources/pi_native/omnigent_pi_native_extension.js"
    )
    env = {k: v for k, v in os.environ.items() if not k.startswith(("PI_", "OMNIGENT_"))}
    env.update(HOME=str(tmp_path), PI_OFFLINE="1")
    result = subprocess.run(
        [
            node,
            str(Path(__file__).with_name("pi-economic-compaction.cjs")),
            pi,
            str(extension),
            str(tmp_path),
        ],
        text=True,
        capture_output=True,
        timeout=45,
        env=env,
        cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr + result.stdout
    assert '"compactions":1' in result.stdout
