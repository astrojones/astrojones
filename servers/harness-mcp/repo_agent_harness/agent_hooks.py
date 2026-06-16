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
import os
import sys
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

_SERENA_GATE_MSG_UNBOARDED = (
    "Read is forbidden for code discovery in this repo. "
    "FIRST call serena_initial_instructions, then run serena_onboarding to write project memories. "
    "Then navigate by symbol (serena_get_symbols_overview / serena_find_symbol). "
    f"(Set {_SERENA_GATE_ENV}=1 to disable this gate.)"
)

_SERENA_GATE_MSG_BOARDED = (
    "Read is forbidden for code discovery in this repo. Navigate by symbol "
    "(serena_get_symbols_overview / serena_find_symbol) instead. "
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


def _serena_gate_blocks(repo: str, path: str) -> tuple[bool, str]:
    """Return (blocks: bool, message: str) to decide if a Read of ``path`` is denied.

    Denies reads of *code* files to force persistent Serena-first navigation. Message
    varies by onboarding status. Fails OPEN (returns False, "") for non-code files,
    paths outside the repo, the env escape, or any error.
    """
    if os.environ.get(_SERENA_GATE_ENV) == "1":
        return False, ""
    try:
        target = Path(path).resolve()
        rootp = Path(repo).resolve()
        if rootp != target and rootp not in target.parents:
            return False, ""  # outside the repo — not our concern
        if target.suffix.lower() not in _CODE_EXTENSIONS:
            return False, ""  # non-code files are always readable
        # Code files are *always* denied; vary message by onboarding status.
        msg = _SERENA_GATE_MSG_UNBOARDED if not _serena_onboarded(rootp) else _SERENA_GATE_MSG_BOARDED
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
    only this module (and git/policies/secrets), not the full CLI graph (gateway, health, verify,
    …) — ~40ms vs ~600ms per tool call. Reads the event JSON on stdin, prints the decision JSON.
    Fail-open by contract: any error prints an empty response and exits 0.
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
