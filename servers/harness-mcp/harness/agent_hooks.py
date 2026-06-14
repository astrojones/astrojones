"""Claude Code hook handlers, exposed via ``repo-agent-harness hook <event>``.

Pure functions: take the hook event payload, return the hook JSON response
(empty dict = allow / no output). The CLI wrapper owns stdin/stdout and
fail-open behavior, so a hook problem never blocks legitimate work.
"""

from __future__ import annotations

from pathlib import Path

from harness import git, policies, secrets

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


def pre_tool_use(data: dict) -> dict:
    """Deny dangerous shell commands and secret-path reads via repo policy."""
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    root = git.repo_root() or Path.cwd()

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
