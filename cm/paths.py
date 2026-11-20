"""Workspace path resolution for Content Machine 3.0."""

from __future__ import annotations

import os
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
