"""Claude Code hook handlers, exposed via ``repo-agent-harness hook <event>``.

Pure functions: take the hook event payload, return the hook JSON response
(empty dict = allow / no output). The CLI wrapper owns stdin/stdout and
fail-open behavior, so a hook problem never blocks legitimate work.
"""

from __future__ import annotations

import os
from pathlib import Path

from repo_agent_harness import git, policies, secrets

_GUARDED_FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_VERIFY_NUDGE = (
    "A file was modified. Before continuing, verify the change: run repo_verify_changed "
    "(or agent/tools/safe-diff then agent/tools/test-changed) to check only what changed."
)


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


# Code-file extensions whose discovery must go through Serena (kept local so the hot-path
# hook never imports the heavier context module). Mirrors context.LANG_BY_EXT.
_CODE_EXTENSIONS = frozenset(
    {
        ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".rb", ".java", ".kt",
        ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".php", ".swift", ".scala", ".dart",
        ".ex", ".exs", ".lua", ".sh",
    }
)

_SERENA_GATE_ENV = "REPO_AGENT_HARNESS_NO_SERENA_GATE"

_SERENA_GATE_MSG = (
    "Read is forbidden for code discovery in a repo that is not yet Serena-onboarded. "
    "FIRST call serena_initial_instructions, then run serena_onboarding to write the project "
    "memories; afterwards navigate by symbol (serena_get_symbols_overview / serena_find_symbol) "
    "and reserve Read for non-code files or a few lines after an overview. "
    f"(Set {_SERENA_GATE_ENV}=1 to disable this gate.)"
)


def _serena_onboarded(rootp: Path) -> bool:
    """Return whether the repo has Serena project memories beyond the scaffolded note."""
    mem_dir = rootp / ".serena" / "memories"
    try:
        return mem_dir.is_dir() and any(
            p.suffix == ".md" and p.stem != "memory_maintenance" for p in mem_dir.iterdir()
        )
    except OSError:
        return True  # fail open: uncertainty must never block a read


def _serena_gate_blocks(repo: str, path: str) -> bool:
    """Return whether a Read of ``path`` must be denied to force Serena-first navigation.

    Blocks reads of *code* files in a repo that has not yet been Serena-onboarded, so agents
    navigate by symbol and complete onboarding first. Fails OPEN (returns ``False``) for non-code
    files, paths outside the repo, the env escape, or any error.
    """
    if os.environ.get(_SERENA_GATE_ENV) == "1":
        return False
    try:
        target = Path(path).resolve()
        rootp = Path(repo).resolve()
        if rootp != target and rootp not in target.parents:
            return False  # outside the repo — not our concern
        if target.suffix.lower() not in _CODE_EXTENSIONS:
            return False  # non-code files are always readable
        return not _serena_onboarded(rootp)
    except OSError:
        return False  # fail open


def pre_tool_use(data: dict) -> dict:
    """Deny dangerous shell commands, secret-path reads, and ungated code reads via repo policy."""
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    repo = git.repo_root()
    root = repo or str(Path.cwd())

    if tool == "Bash":
        cmd = tin.get("command", "")
        if cmd:
            check = policies.check_command(cmd, root)
            if not check.allowed:
                return _deny(check.reason)

    elif tool in _GUARDED_FILE_TOOLS:
        path = tin.get("file_path") or tin.get("path") or tin.get("notebook_path") or ""
        if path:
            cfg = secrets.load(root)
            try:
                rel = str(Path(path).resolve().relative_to(Path(root).resolve()))
            except ValueError:
                rel = path
            if secrets.is_secret_path(rel, cfg):
                return _deny(f"Accessing a secret path ('{rel}') is blocked by policy.")
            if tool == "Read" and repo is not None and _serena_gate_blocks(repo, path):
                return _deny(_SERENA_GATE_MSG)

    return {}


def post_tool_use(data: dict) -> dict:
    """After an edit/write, nudge the agent to verify the change."""
    if data.get("tool_name", "") in _EDIT_TOOLS:
        return {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": _VERIFY_NUDGE,
            }
        }
    return {}
