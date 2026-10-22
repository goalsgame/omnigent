"""Operator library validation and content-addressed agent delivery."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from omnigent.server import app as server_app
from omnigent.server.operator_skills import OperatorSkills
from tests.server.test_app import _independent_seed_stores


def _skill(body: str = "Review the requested code.") -> dict[str, str]:
    return {
        "SKILL.md": "---\nname: code-review\ndescription: Review code changes\n---\n" + body,
        "references/checklist.md": "Inspect caller contracts.",
    }


def _source(tmp_path: Path, *, harness: str = "pi-native") -> Path:
    source = tmp_path / "assistant.yaml"
    source.write_text(
        f"name: assistant\nexecutor:\n  harness: {harness}\n"
        "  model: test-model\nprompt: Preserve this user prompt.\n"
    )
    return source


def _config(files: dict[str, str] | None = None) -> dict:
    return {
        "library": {"code-review": files or _skill()},
        "agents": {"assistant": ["code-review"]},
    }


@pytest.mark.parametrize("harness", ["pi-native", "claude-sdk"])
def test_operator_skills_seed_update_remove_preserves_agent_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, harness: str
) -> None:
    source = _source(tmp_path, harness=harness)
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(source))
    stores = _independent_seed_stores(tmp_path, "operator")

    def seed(config: object):
        parsed = OperatorSkills.parse(config, sources={"assistant": source})
        server_app._ensure_extra_builtin_agents(
            stores.agent_store, stores.artifact_store, stores.agent_cache, parsed
        )
        row = stores.agent_store.get_by_name("assistant")
        assert row is not None
        return row, stores.agent_cache.load(row.id, row.bundle_location).spec

    first, spec = seed(_config())
    assert spec.instructions == "Preserve this user prompt."
    assert [skill.name for skill in spec.skills] == ["code-review"]
    skill = spec.skills[0]
    assert (skill.skill_dir / "references/checklist.md").read_text() == "Inspect caller contracts."
    unchanged, _ = seed(_config())
    assert unchanged.bundle_location == first.bundle_location
    updated, spec = seed(_config(_skill("Use the updated review procedure.")))
    assert updated.id == first.id
    assert updated.bundle_location != first.bundle_location
    assert "updated review procedure" in spec.skills[0].content
    removed, spec = seed(None)
    assert removed.id == first.id
    assert removed.bundle_location not in {first.bundle_location, updated.bundle_location}
    assert spec.skills == []
    assert spec.instructions == "Preserve this user prompt."


@pytest.mark.parametrize(
    "path",
    ["../outside", "/absolute", "C:/windows", "references\\file.md", "a/./b", "a//b", "a/../b"],
)
def test_operator_skills_rejects_unsafe_paths(tmp_path: Path, path: str) -> None:
    source = _source(tmp_path)
    files = _skill()
    files[path] = "unsafe"
    with pytest.raises(ValueError, match="path"):
        OperatorSkills.parse(_config(files), sources={"assistant": source})
    assert not (tmp_path / "outside").exists()


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"unexpected": {}},
        {"library": []},
        {"library": {"bad/id": _skill()}},
        {"library": {"code-review": {"reference.md": "missing entrypoint"}}},
        {"library": {"code-review": {"SKILL.md": 42}}},
        {"agents": {"unknown": []}},
        {"agents": {"assistant": ["unknown"]}},
        {"agents": {"assistant": "code-review"}},
        {
            "library": {"code-review": _skill()},
            "agents": {"assistant": ["code-review", "code-review"]},
        },
        {"library_dirs": "not-a-list"},
        {"library_dirs": [""]},
        {"library_dirs": ["relative/skills"]},
    ],
)
def test_operator_skills_rejects_malformed_config(tmp_path: Path, raw: object) -> None:
    with pytest.raises(ValueError):
        OperatorSkills.parse(raw, sources={"assistant": _source(tmp_path)})


def test_operator_skills_validates_entrypoint_with_actual_parser(tmp_path: Path) -> None:
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError, match="frontmatter"):
        OperatorSkills.parse(
            _config({"SKILL.md": "not a skill"}), sources={"assistant": _source(tmp_path)}
        )


@pytest.mark.parametrize("kind", ["collision", "case-collision", "symlink"])
def test_operator_skills_rejects_bundled_collision_and_links(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "assistant"
    source.mkdir()
    (source / "config.yaml").write_text("name: assistant\nprompt: hi\n")
    skills = source / "skills"
    skills.mkdir()
    if kind == "symlink":
        (skills / "external").symlink_to(tmp_path / "elsewhere")
    else:
        (skills / ("code-review" if kind == "collision" else "CODE-REVIEW")).mkdir()
    with pytest.raises(ValueError, match=r"collides|linked"):
        OperatorSkills.parse(_config(), sources={"assistant": source})


def test_operator_skills_size_limits(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="128 KiB"):
        OperatorSkills.parse(
            _config(_skill("x" * (128 * 1024))), sources={"assistant": _source(tmp_path)}
        )


def test_create_app_rejects_operator_skills_before_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stores = _independent_seed_stores(tmp_path, "startup")
    source = _source(tmp_path)
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(source))
    with pytest.raises(ValueError, match="unknown"):
        server_app.create_app(
            agent_store=stores.agent_store,
            artifact_store=stores.artifact_store,
            conversation_store=None,
            file_store=None,
            agent_cache=stores.agent_cache,
            server_config={"operator_skills": {"agents": {"assistant": ["missing"]}}},
        )


def test_direct_yaml_load_does_not_import_host_sibling_skills(tmp_path: Path) -> None:
    from omnigent.spec import load

    source = _source(tmp_path)
    destination = tmp_path / "skills/code-review"
    destination.mkdir(parents=True)
    (destination / "SKILL.md").write_text(_skill()["SKILL.md"])
    assert load(source).skills == []
    assert [s.name for s in load(tmp_path).skills] == ["code-review"]


def test_native_pi_launch_exposes_bundle_skill_and_resources(tmp_path: Path) -> None:
    from omnigent.runner.native.orchestration import _build_pi_native_args
    from omnigent.spec import load

    source = _source(tmp_path)
    configured = OperatorSkills.parse(_config(), sources={"assistant": source})
    from omnigent.spec import materialize_bundle

    bundle = materialize_bundle(source, tmp_path / "bundle")
    configured.apply(bundle, "assistant")
    spec = load(bundle)
    args = _build_pi_native_args(
        terminal_launch_args=None,
        extension_path=tmp_path / "extension.mjs",
        session_dir=tmp_path / "sessions",
        external_session_id=None,
        bundle_dir=bundle,
        skills_filter=spec.skills_filter,
    )
    skill_dir = Path(args[args.index("--skill") + 1])
    assert skill_dir == bundle / "skills/code-review"
    assert (skill_dir / "references/checklist.md").is_file()


def test_operator_skills_rejects_invalid_mapped_agent(tmp_path: Path) -> None:
    source = _source(tmp_path)
    source.write_text("name: assistant\nexecutor:\n  harness: unsupported-harness\nprompt: hi\n")
    from omnigent.errors import OmnigentError

    with pytest.raises(OmnigentError, match=r"harness|supported"):
        OperatorSkills.parse(_config(), sources={"assistant": source})


def test_operator_skills_allows_existing_underscore_agent_name(tmp_path: Path) -> None:
    source = _source(tmp_path)
    config = _config()
    config["agents"] = {"custom_agent": ["code-review"]}
    parsed = OperatorSkills.parse(config, sources={"custom_agent": source})
    assert parsed.agents["custom_agent"] == ("code-review",)


@pytest.mark.parametrize("length", [1024, 1025])
def test_operator_skill_description_matches_bundle_validation(tmp_path: Path, length: int) -> None:
    from omnigent.errors import OmnigentError

    files = {"SKILL.md": "---\nname: code-review\ndescription: " + "x" * length + "\n---\nReview."}
    if length == 1024:
        OperatorSkills.parse(_config(files), sources={"assistant": _source(tmp_path)})
    else:
        with pytest.raises((ValueError, OmnigentError), match=r"description|1024"):
            OperatorSkills.parse(_config(files), sources={"assistant": _source(tmp_path)})


def test_operator_skills_rejects_total_library_size(tmp_path: Path) -> None:
    files = _skill()
    for index in range(8):
        files[f"references/large-{index}.md"] = "x" * (128 * 1024)
    with pytest.raises(ValueError, match="900 KiB"):
        OperatorSkills.parse(_config(files), sources={"assistant": _source(tmp_path)})


@pytest.mark.parametrize("paths", [("a", "a/b"), ("A/b", "a"), ("A.md", "a.md")])
def test_operator_skills_rejects_conflicting_resources(
    tmp_path: Path, paths: tuple[str, str]
) -> None:
    files = _skill()
    files.update(dict.fromkeys(paths, "resource"))
    with pytest.raises(ValueError, match=r"colliding|conflicting"):
        OperatorSkills.parse(_config(files), sources={"assistant": _source(tmp_path)})


def test_operator_skills_rejects_frontmatter_name_mismatch(tmp_path: Path) -> None:
    files = _skill()
    files["SKILL.md"] = files["SKILL.md"].replace("name: code-review", "name: different-name")
    with pytest.raises(ValueError, match="name must equal"):
        OperatorSkills.parse(_config(files), sources={"assistant": _source(tmp_path)})


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_env", [False, True])
async def test_mapped_yaml_disappearing_after_validation_fails_lifespan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime_init: None, remove_env: bool
) -> None:
    from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
    from omnigent.stores.file_store.sqlalchemy_store import SqlAlchemyFileStore

    source = _source(tmp_path)
    stores = _independent_seed_stores(tmp_path, "disappearing")
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(source))
    for helper in (
        "_ensure_default_native_agents",
        "_ensure_default_acp_agents",
        "_ensure_default_debby_agent",
        "_ensure_default_polly_agent",
    ):
        monkeypatch.setattr(server_app, helper, lambda *args: None)
    uri = f"sqlite:///{tmp_path / 'disappearing.db'}"
    app = server_app.create_app(
        agent_store=stores.agent_store,
        artifact_store=stores.artifact_store,
        agent_cache=stores.agent_cache,
        conversation_store=SqlAlchemyConversationStore(uri),
        file_store=SqlAlchemyFileStore(uri),
        server_config={"operator_skills": _config()},
    )
    source.unlink()
    if remove_env:
        monkeypatch.delenv(server_app._EXTRA_BUILTIN_AGENTS_ENV)
    try:
        with pytest.raises(FileNotFoundError, match="source not found"):
            async with app.router.lifespan_context(app):
                pytest.fail("Mapped agent disappearance must abort startup")
    finally:
        await app.state.harness_process_manager.shutdown()
    assert stores.agent_store.get_by_name("assistant") is None


def _library_dir(tmp_path: Path, files: dict[str, str] | None = None) -> Path:
    root = tmp_path / "skills"
    for relative, text in (files or _skill()).items():
        path = root / "code-review" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def _dirs_config(*roots: Path, library: dict | None = None) -> dict:
    config: dict = {
        "library_dirs": [str(root) for root in roots],
        "agents": {"assistant": ["code-review"]},
    }
    if library is not None:
        config["library"] = library
    return config


def test_operator_skills_reads_library_directories(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    (root / "code-review" / ".hidden.md").write_text("ignored")
    (root / ".DS_Store").write_text("ignored")
    parsed = OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})
    assert parsed.library == {"code-review": _skill()}
    assert parsed.agents == {"assistant": ("code-review",)}


def test_operator_skills_reads_configmap_style_mounts(tmp_path: Path) -> None:
    mount = tmp_path / "skills" / "code-review"
    data = mount / "..2026_10_22_12_00_00.123"
    for relative, text in _skill().items():
        path = data / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (mount / "..data").symlink_to(data.name)
    (mount / "SKILL.md").symlink_to(Path("..data") / "SKILL.md")
    (mount / "references").symlink_to(Path("..data") / "references")
    parsed = OperatorSkills.parse(
        _dirs_config(mount.parent), sources={"assistant": _source(tmp_path)}
    )
    assert parsed.library == {"code-review": _skill()}


def test_operator_skills_library_directory_seeds_like_inline_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _source(tmp_path)
    monkeypatch.setenv(server_app._EXTRA_BUILTIN_AGENTS_ENV, str(source))
    stores = _independent_seed_stores(tmp_path, "operator-dirs")
    root = _library_dir(tmp_path)
    parsed = OperatorSkills.parse(_dirs_config(root), sources={"assistant": source})
    server_app._ensure_extra_builtin_agents(
        stores.agent_store, stores.artifact_store, stores.agent_cache, parsed
    )
    row = stores.agent_store.get_by_name("assistant")
    assert row is not None
    spec = stores.agent_cache.load(row.id, row.bundle_location).spec
    assert [skill.name for skill in spec.skills] == ["code-review"]
    assert (spec.skills[0].skill_dir / "references/checklist.md").read_text() == (
        "Inspect caller contracts."
    )


def test_operator_skills_rejects_missing_library_directory(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a directory"):
        OperatorSkills.parse(
            _dirs_config(tmp_path / "absent"), sources={"assistant": _source(tmp_path)}
        )


def test_operator_skills_rejects_skill_in_two_libraries(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    with pytest.raises(ValueError, match="more than one library"):
        OperatorSkills.parse(
            _dirs_config(root, library={"code-review": _skill()}),
            sources={"assistant": _source(tmp_path)},
        )
    other = _library_dir(tmp_path / "other")
    with pytest.raises(ValueError, match="more than one library"):
        OperatorSkills.parse(_dirs_config(root, other), sources={"assistant": _source(tmp_path)})


def test_operator_skills_rejects_library_file_outside_its_directory(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    outside = tmp_path / "outside.md"
    outside.write_text("leaked")
    (root / "code-review" / "references" / "leak.md").symlink_to(outside)
    with pytest.raises(ValueError, match="inside their skill directory"):
        OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})


def test_operator_skills_rejects_binary_library_file(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    (root / "code-review" / "references" / "image.png").write_bytes(b"\x89PNG\xff\xfe")
    with pytest.raises(ValueError, match="valid UTF-8"):
        OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})


def test_operator_skills_library_directory_requires_entrypoint(tmp_path: Path) -> None:
    root = _library_dir(tmp_path, {"references/checklist.md": "no entrypoint"})
    with pytest.raises(ValueError, match=r"SKILL\.md"):
        OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})


def test_operator_skills_rejects_cyclic_library_links(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    (root / "code-review" / "references" / "loop").symlink_to("..")
    with pytest.raises(ValueError, match="must not link to themselves"):
        OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})


def test_operator_skills_accepts_sibling_directory_alias(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    (root / "code-review" / "refs").symlink_to("references")
    parsed = OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})
    assert parsed.library["code-review"] == {
        **_skill(),
        "refs/checklist.md": "Inspect caller contracts.",
    }


def test_operator_skills_rejects_oversized_library_file(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    (root / "code-review" / "references" / "large.md").write_text("x" * (128 * 1024 + 1))
    with pytest.raises(ValueError, match="128 KiB"):
        OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})


def test_operator_skills_rejects_unreadable_library_directory(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    references = root / "code-review" / "references"
    references.chmod(0)
    try:
        if os.access(references, os.R_OK):
            pytest.skip("directory permissions are not enforced for this user")
        with pytest.raises(ValueError, match="cannot read library directory"):
            OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})
    finally:
        references.chmod(0o755)


@pytest.mark.parametrize("limit", ["files", "bytes"])
def test_operator_skills_stops_reading_library_at_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    root = _library_dir(tmp_path)
    references = root / "code-review" / "references"
    count, body = (600, "x") if limit == "files" else (20, "x" * (100 * 1024))
    for index in range(count):
        (references / f"r{index:04}.md").write_text(body)
    reads: list[Path] = []
    original = Path.open

    def counting_open(self: Path, *args, **kwargs):
        reads.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", counting_open)
    with pytest.raises(ValueError, match="512 files" if limit == "files" else "900 KiB"):
        OperatorSkills.parse(_dirs_config(root), sources={"assistant": _source(tmp_path)})
    assert len(reads) < count


def test_operator_skills_budget_counts_inline_skills(tmp_path: Path) -> None:
    root = _library_dir(tmp_path)
    inline = {
        f"inline-{index}": {
            "SKILL.md": f"---\nname: inline-{index}\ndescription: Inline\n---\nBody"
        }
        for index in range(128)
    }
    with pytest.raises(ValueError, match="128 skills"):
        OperatorSkills.parse(
            _dirs_config(root, library=inline), sources={"assistant": _source(tmp_path)}
        )
