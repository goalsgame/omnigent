"""Validated operator-owned skill libraries for extra built-in agent bundles."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from omnigent.spec.parser import _parse_skill

_NAME = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
_AGENT_NAME = re.compile(r"[a-zA-Z0-9_-]+\Z")
_MAX_BYTES = 900 * 1024
_MAX_FILE_BYTES = 128 * 1024


def _name(value: object, label: str) -> str:
    if not isinstance(value, str) or len(value) > 64 or not _NAME.fullmatch(value):
        raise ValueError(f"operator_skills {label} must be a lowercase hyphenated name")
    return value


def _path(value: object) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("operator_skills file path must be a nonempty relative POSIX path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or "\\" in value
        or ":" in value
        or any(ord(char) < 32 for char in value)
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        raise ValueError("operator_skills file path must be a safe relative POSIX path")
    return value


def _library_from_dirs(raw: object) -> dict[str, dict[str, str]]:
    """Read ``<dir>/<skill-id>/**`` text files into the inline library shape."""
    if not isinstance(raw, list) or any(not isinstance(item, str) or not item for item in raw):
        raise ValueError("operator_skills library_dirs must be a list of directory paths")
    library: dict[str, dict[str, str]] = {}
    for entry in raw:
        root = Path(entry)
        if not root.is_absolute():
            raise ValueError(f"operator_skills library directory {entry!r} must be absolute")
        if not root.is_dir():
            raise ValueError(f"operator_skills library directory {entry!r} is not a directory")
        for skill_dir in sorted(root.iterdir()):
            if skill_dir.name.startswith(".") or not skill_dir.is_dir():
                continue
            if skill_dir.name in library:
                raise ValueError(
                    f"operator_skills skill {skill_dir.name!r} appears in more than one library"
                )
            library[skill_dir.name] = _read_skill_dir(skill_dir)
    return library


def _read_skill_dir(skill_dir: Path) -> dict[str, str]:
    # ConfigMap mounts duplicate every file under dot-prefixed ``..data`` directories.
    base = skill_dir.resolve()
    files: dict[str, str] = {}
    visited: set[Path] = set()

    def _fail(error: OSError) -> None:
        raise ValueError(f"operator_skills cannot read library directory: {error}") from error

    for dirpath, dirnames, filenames in os.walk(skill_dir, onerror=_fail, followlinks=True):
        real = Path(dirpath).resolve()
        if real in visited:
            raise ValueError("operator_skills library directories must not link to themselves")
        visited.add(real)
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            path = Path(dirpath, filename)
            if not path.is_file():
                continue
            if not path.resolve().is_relative_to(base):
                raise ValueError(
                    "operator_skills library files must stay inside their skill directory"
                )
            with path.open("rb") as handle:
                data = handle.read(_MAX_FILE_BYTES + 1)
            if len(data) > _MAX_FILE_BYTES:
                raise ValueError("operator_skills each file must be at most 128 KiB")
            try:
                text = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError("operator_skills files must contain valid UTF-8 text") from exc
            files[path.relative_to(skill_dir).as_posix()] = text
    return files


@dataclass(frozen=True)
class OperatorSkills:
    """A bounded text library and explicit per-agent selections."""

    library: dict[str, dict[str, str]]
    agents: dict[str, tuple[str, ...]]
    sources: dict[Path, str] = field(default_factory=dict)

    @classmethod
    def parse(cls, raw: object, *, sources: dict[str, Path]) -> OperatorSkills:
        """Validate configuration and selected source bundles before startup."""
        if raw is None:
            return cls({}, {})
        if not isinstance(raw, dict) or set(raw) - {"library", "library_dirs", "agents"}:
            raise ValueError(
                "operator_skills must contain only library, library_dirs and agents entries"
            )
        library_raw = raw.get("library", {})
        agents_raw = raw.get("agents", {})
        if not isinstance(library_raw, dict) or not isinstance(agents_raw, dict):
            raise ValueError("operator_skills library and agents must be mappings")
        if "library_dirs" in raw:
            from_dirs = _library_from_dirs(raw["library_dirs"])
            if shared := set(from_dirs) & set(library_raw):
                raise ValueError(
                    f"operator_skills skill {sorted(shared)[0]!r} appears in more than one library"
                )
            library_raw = {**library_raw, **from_dirs}
        if len(library_raw) > 128 or len(agents_raw) > 128:
            raise ValueError("operator_skills supports at most 128 skills and agent mappings")
        library: dict[str, dict[str, str]] = {}
        file_count = 0
        for key, files in library_raw.items():
            skill_id = _name(key, "skill ID")
            if not isinstance(files, dict) or "SKILL.md" not in files:
                raise ValueError("operator_skills each skill requires a SKILL.md text file")
            copied: dict[str, str] = {}
            for path, text in files.items():
                relative = _path(path)
                if not isinstance(text, str) or "\x00" in text:
                    raise ValueError("operator_skills files must contain UTF-8 text without NUL")
                try:
                    size = len(text.encode("utf-8"))
                except UnicodeEncodeError as exc:
                    raise ValueError(
                        "operator_skills files must contain valid UTF-8 text"
                    ) from exc
                if size > _MAX_FILE_BYTES:
                    raise ValueError("operator_skills each file must be at most 128 KiB")
                if relative.casefold() in {item.casefold() for item in copied}:
                    raise ValueError("operator_skills contains case-colliding file paths")
                lowered = relative.casefold()
                if any(
                    lowered.startswith(item.casefold() + "/")
                    or item.casefold().startswith(lowered + "/")
                    for item in copied
                ):
                    raise ValueError(
                        "operator_skills contains conflicting file and directory paths"
                    )
                copied[relative] = text
            file_count += len(copied)
            library[skill_id] = copied
        if file_count > 512:
            raise ValueError("operator_skills supports at most 512 files")
        agents: dict[str, tuple[str, ...]] = {}
        for key, selected in agents_raw.items():
            if not isinstance(key, str) or not _AGENT_NAME.fullmatch(key):
                raise ValueError("operator_skills agent name must match [a-zA-Z0-9_-]+")
            name = key
            if name not in sources:
                raise ValueError(f"operator_skills references unknown extra agent {name!r}")
            if not isinstance(selected, list) or any(not isinstance(s, str) for s in selected):
                raise ValueError("operator_skills agent selections must be lists of skill IDs")
            if len(selected) != len(set(selected)) or any(s not in library for s in selected):
                raise ValueError(
                    "operator_skills agent selections contain duplicate or unknown skills"
                )
            agents[name] = tuple(selected)
        serialized = json.dumps({"library": library, "agents": agents}, ensure_ascii=False)
        if len(serialized.encode("utf-8")) > _MAX_BYTES:
            raise ValueError("operator_skills configuration must be at most 900 KiB")
        result = cls(
            library,
            agents,
            {path.absolute(): name for name, path in sources.items() if name in agents},
        )
        from omnigent.spec import load as load_spec
        from omnigent.spec import materialize_bundle

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for skill_id in library:
                result._write_skill(root / "library" / skill_id, skill_id)
            for name in agents:
                source = sources[name]
                result.check_source(source)
                bundle = materialize_bundle(source, root / "agents" / name)
                result.apply(bundle, name)
                load_spec(bundle, expand_env=False)
        return result

    def check_source(self, source: Path) -> None:
        """Reject linked bundled skills before materialization follows links."""
        skills = source / "skills"
        if source.is_dir() and (
            skills.is_symlink()
            or (skills.exists() and any(path.is_symlink() for path in skills.rglob("*")))
        ):
            raise ValueError("operator_skills cannot augment a bundle containing linked skills")

    def _write_skill(self, dest: Path, skill_id: str) -> None:
        for relative, text in sorted(self.library[skill_id].items()):
            path = dest / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        parsed = _parse_skill(dest / "SKILL.md")
        if parsed.display_name is not None:
            raise ValueError("operator_skills frontmatter name must equal its skill ID")
        if not parsed.description.strip():
            raise ValueError("operator_skills skill description must not be empty")
        from omnigent.spec.types import AgentSpec
        from omnigent.spec.validator import ValidationResult, _validate_skills

        result = ValidationResult()
        _validate_skills(AgentSpec(spec_version=1, skills=[parsed]), result)
        if not result.valid:
            raise ValueError(
                "operator_skills invalid skill: "
                + "; ".join(f"{error.path}: {error.message}" for error in result.errors)
            )

    def apply(self, bundle: Path, name: str) -> None:
        """Add selected skill resources without replacing bundled content."""
        selected = self.agents.get(name, ())
        if not selected:
            return
        skills = bundle / "skills"
        if skills.is_symlink() or (skills.exists() and not skills.is_dir()):
            raise ValueError("operator_skills destination skills must be an ordinary directory")
        existing = (
            {path.name.casefold() for path in skills.iterdir()} if skills.exists() else set()
        )
        for skill_id in selected:
            if skill_id.casefold() in existing:
                raise ValueError(f"operator_skills collides with bundled skill {skill_id!r}")
            self._write_skill(skills / skill_id, skill_id)
