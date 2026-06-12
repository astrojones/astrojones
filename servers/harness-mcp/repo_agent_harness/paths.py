"""Stable paths for harness persistent state (~/.harness by default)."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path


def repo_id(root: str) -> str:
    """Short stable identifier for the repo (SHA-256 of realpath, first 12 hex chars)."""
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]


def harness_home() -> Path:
    """Root of harness persistent state; override via REPO_AGENT_HARNESS_HOME."""
    env = os.environ.get("REPO_AGENT_HARNESS_HOME")
    return Path(env) if env else Path.home() / ".harness"


def repo_state_dir(root: str) -> Path:
    """Per-repo state dir under harness_home(); created lazily with mode 0700."""
    d = harness_home() / "repos" / repo_id(root)
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d
