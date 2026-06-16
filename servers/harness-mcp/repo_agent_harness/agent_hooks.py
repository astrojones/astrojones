"""Claude Code hook handlers, exposed via ``repo-agent-harness hook <event>``.

Pure functions: take the hook event payload, return the hook JSON response
(empty dict = allow / no output). The CLI wrapper (``repo-agent-harness hook``)
or the lightweight ``main`` below — invoked as ``python -m
repo_agent_harness.agent_hooks <event>`` by the plugin hook to skip the heavy
CLI import — owns stdin/stdout and fail-open behavior, so a hook problem never
blocks legitimate work.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from repo_agent_harness import git, policies, secrets, serena_gate

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


def _serena_gate_blocks(repo: str, path: str) -> tuple[bool, str]:
    """Return (blocks, message) deciding whether a native Read of ``path`` is denied.

    Denies reads of *code* files to keep code discovery on Serena; the message varies by
    onboarding status (serena_gate.UNBOARDED_MSG vs BOARDED_MSG). Fails OPEN for non-code
    files, paths outside the repo, the env escape, or any error. The same predicate gates
    repo_read_range in server.py, so no ungated whole-file code path is left open.
    """
    if serena_gate.gate_disabled():
        return False, ""
    try:
        target = Path(path).resolve()
        rootp = Path(repo).resolve()
        if rootp != target and rootp not in target.parents:
            return False, ""  # outside the repo — not our concern
        if not serena_gate.is_code_file(target):
            return False, ""  # non-code files are always readable
        msg = serena_gate.UNBOARDED_MSG if not serena_gate.is_onboarded(rootp) else serena_gate.BOARDED_MSG
        return True, msg  # noqa: TRY300 — intentional return in try; fallthrough catch is fail-open
    except OSError:
        return False, ""  # fail open


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
            if tool == "Read" and repo is not None:
                blocks, msg = _serena_gate_blocks(repo, path)
                if blocks:
                    return _deny(msg)

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


def main(argv: list[str] | None = None) -> int:
    """Lightweight hook entry: ``python -m repo_agent_harness.agent_hooks <event>``.

    The plugin's PreToolUse shim calls this instead of ``repo-agent-harness hook`` so it imports
    only this module (and git/policies/secrets/serena_gate), not the full CLI graph (gateway,
    health, verify, …) — ~40ms vs ~600ms per tool call. Reads the event JSON on stdin, prints the
    decision JSON. Fail-open by contract: any error prints an empty response and exits 0.
    """
    args = sys.argv[1:] if argv is None else argv
    event = args[0] if args else "pre-tool-use"
    try:
        data = json.load(sys.stdin)
        out = pre_tool_use(data) if event == "pre-tool-use" else post_tool_use(data)
    except Exception:  # noqa: BLE001 — fail-open contract: any error must yield an empty allow
        out = {}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
