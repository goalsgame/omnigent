"""Bounded publication reads confined to a pinned workspace directory."""

from __future__ import annotations

import os
import stat
from pathlib import Path


def read_publication_file(path: str, workspace: Path, maximum: int) -> tuple[str, bytes]:
    """Read a regular file without following symlinks in any path component.

    Platforms without no-follow directory-relative opens fail closed; callers
    can publish bytes through the authenticated HTTP API instead.
    """
    if not (
        os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY")
    ):
        raise ValueError("Confined publication reads are unavailable on this platform")
    relative = Path(path)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("Publishing requires a workspace-relative file path")
    root = Path(os.path.abspath(workspace))
    directory_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    descriptor = os.open(root.anchor, directory_flags)
    try:
        for name in (*root.parts[1:], *relative.parts[:-1]):
            child = os.open(name, directory_flags, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        leaf = os.open(
            relative.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
            dir_fd=descriptor,
        )
        with os.fdopen(leaf, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise ValueError("Publishing requires a regular file")
            content = source.read(maximum + 1)
            if len(content) > maximum:
                raise ValueError("Artifact exceeds the runner upload limit")
            return relative.name, content
    finally:
        os.close(descriptor)
