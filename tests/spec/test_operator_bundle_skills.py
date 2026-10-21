"""Directory-shaped legacy YAML bundles obey the normal skill invariants."""

from pathlib import Path

import pytest

from omnigent.errors import OmnigentError
from omnigent.spec import load


@pytest.mark.parametrize("invalid", ["description", "name", "duplicate"])
def test_yaml_bundle_revalidates_discovered_skills(tmp_path: Path, invalid: str) -> None:
    (tmp_path / "assistant.yaml").write_text(
        "name: assistant\nexecutor:\n  harness: pi-native\nprompt: hi\n"
    )
    skill_name = "x" * 65 if invalid == "name" else "review"
    desc = "x" * 1025 if invalid == "description" else "Review a change"
    for namespace in ["first", "second"] if invalid == "duplicate" else ["first"]:
        directory = tmp_path / "skills" / namespace / skill_name
        directory.mkdir(parents=True)
        (directory / "SKILL.md").write_text(
            f"---\nname: {skill_name}\ndescription: {desc}\n---\nReview."
        )
    with pytest.raises(OmnigentError, match=r"skills|skill"):
        load(tmp_path)
