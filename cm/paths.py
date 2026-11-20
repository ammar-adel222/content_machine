"""Workspace path resolution and atomic file utilities for Content Machine 3.0."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def get_repo_root() -> Path:
    """Resolve the harness root directory, honoring CM_ROOT for test isolation."""
    env_root = os.environ.get("CM_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()

    cwd = Path.cwd().resolve()
    for candidate in (cwd, *cwd.parents):
        if (candidate / "pyproject.toml").is_file():
            return candidate

    return Path(__file__).resolve().parent.parent


def atomic_write_text(path: Path, content: str) -> Path:
    """Atomically write UTF-8 text via a same-directory temp file and os.replace."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            tmp_path = Path(handle.name)
            handle.write(content)
            handle.flush()
        os.replace(tmp_path, path)
    except Exception:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)
        raise
    return path


def runs_dir(root: Path | None = None) -> Path:
    """Return the directory storing per-video run workspaces."""
    return (root or get_repo_root()) / "runs"


def run_dir(slug: str, root: Path | None = None) -> Path:
    """Return the workspace directory for a specific video slug."""
    return runs_dir(root) / slug


def channels_dir(root: Path | None = None) -> Path:
    """Return the directory storing channel catalog metadata."""
    return (root or get_repo_root()) / "channels"


def catalog_path(root: Path | None = None) -> Path:
    """Return the path to channels/catalog.json."""
    return channels_dir(root) / "catalog.json"


def assets_dir(root: Path | None = None) -> Path:
    """Return the directory storing audio and visual assets."""
    return (root or get_repo_root()) / "assets"


def engine_dir(root: Path | None = None) -> Path:
    """Return the directory storing the 2D visual engine."""
    return (root or get_repo_root()) / "engine"


def resolve_engine_dir(root: Path | None = None) -> Path:
    """Return the active engine directory, falling back to the package repo engine."""
    candidate = engine_dir(root)
    if (candidate / "runtime.js").is_file():
        return candidate
    return Path(__file__).resolve().parent.parent / "engine"
