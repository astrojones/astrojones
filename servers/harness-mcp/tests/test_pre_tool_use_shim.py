"""End-to-end tests for the PreToolUse shim (``hooks/pre_tool_use.py``).

These drive the REAL shim script as a subprocess — the integration path the unit tests on
``agent_hooks.pre_tool_use`` do NOT exercise. The shim resolves a *trusted* harness and dispatches
the event to it; a regression that makes it fail open (the dormant-gate bug, where the guard only
fired for sha-pinned repos) shows up only here, never in the direct-call unit tests.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SHIM = _REPO_ROOT / "hooks" / "pre_tool_use.py"
_BUNDLE_VENV = _REPO_ROOT / "servers" / "harness-mcp" / ".venv" / "bin" / "python"

# The shim runs the bundled harness via the project venv (or `uv run` cold). Skip only if neither
# route exists, so the suite stays green in a stripped environment.
_runnable = _SHIM.is_file() and (_BUNDLE_VENV.is_file() or shutil.which("uv") is not None)
pytestmark = pytest.mark.skipif(not _runnable, reason="no shim, bundle venv, or uv to run the harness")


def _git_repo(path: Path, *, onboarded: bool) -> Path:
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "foo.py").write_text("x = 1\n")
    (path / "notes.md").write_text("# notes\n")
    (path / ".env").write_text("SECRET=1\n")
    if onboarded:
        mem = path / ".serena" / "memories"
        mem.mkdir(parents=True)
        (mem / "core.md").write_text("core\n")
    return path


def _drive(repo: Path, payload: dict, env_extra: dict[str, str] | None = None) -> dict:
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(repo), **(env_extra or {})}
    out = subprocess.run(
        [sys.executable, str(_SHIM)],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        cwd=str(repo),
        env=env,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout or "{}")


def _denied(decision: dict) -> bool:
    return decision.get("hookSpecificOutput", {}).get("permissionDecision") == "deny"


def _read(repo: Path, name: str) -> dict:
    return {"tool_name": "Read", "tool_input": {"file_path": str(repo / name)}}


def test_shim_denies_code_read_when_not_onboarded(tmp_path):
    repo = _git_repo(tmp_path, onboarded=False)
    assert _denied(_drive(repo, _read(repo, "foo.py")))


def test_shim_denies_code_read_even_when_onboarded(tmp_path):
    """Persistent Serena preference: code reads denied regardless of onboarding status."""
    repo = _git_repo(tmp_path, onboarded=True)
    assert _denied(_drive(repo, _read(repo, "foo.py")))


def test_shim_allows_non_code_read(tmp_path):
    repo = _git_repo(tmp_path, onboarded=False)
    assert not _denied(_drive(repo, _read(repo, "notes.md")))


def test_shim_restores_secret_guard(tmp_path):
    repo = _git_repo(tmp_path, onboarded=False)
    assert _denied(_drive(repo, _read(repo, ".env")))


def test_shim_restores_bash_guard(tmp_path):
    repo = _git_repo(tmp_path, onboarded=False)
    assert _denied(_drive(repo, {"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}))


def test_shim_env_escape_allows_code_read(tmp_path):
    repo = _git_repo(tmp_path, onboarded=False)
    decision = _drive(repo, _read(repo, "foo.py"), {"REPO_AGENT_HARNESS_NO_SERENA_GATE": "1"})
    assert not _denied(decision)
