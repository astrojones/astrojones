#!/usr/bin/env python3
"""Shared hook shim: pipe a Claude Code hook event through the *trusted* bundled harness.

Generalizes the resolution logic of ``pre_tool_use.py`` (which stays standalone for the
security-critical PreToolUse path) for the non-blocking events whose brain also lives in the
harness package — PostToolUse (perception-aware verify feedback) and UserPromptSubmit (the
per-turn perception digest). The brain is the ``repo_agent_harness.agent_hooks`` module; this
shim only resolves *which* harness to ask, in trust order:

1. The repo's own ``.mcp.json`` entry, only if it is the canonical sha-pinned uvx form.
2. Otherwise the plugin's bundled harness, resolved from this script's own install location
   (``<plugin_root>/servers/harness-mcp``) — trusted by construction, not repo-controlled.

Fail-open by contract: any error prints an empty response and exits 0, so a hook problem never
blocks work. stdlib-only: this runs before/independently of the harness venv.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_GIT_TIMEOUT = 2
_TIMEOUT = 7  # with git's 2s this stays under the 10s budget in hooks.json
_SPEC_RE = re.compile(
    r"^git\+https://github\.com/astrojones/astrojones@[0-9a-f]{40}#subdirectory=servers/harness-mcp$"
)


def _empty() -> None:
    print(json.dumps({}))
    sys.exit(0)


def _harness_argv(root: Path, event: str) -> list[str] | None:
    """Resolve the repo's own sha-pinned harness from its ``.mcp.json``, if trusted (else None)."""
    try:
        cfg = json.loads((root / ".mcp.json").read_text())
        entry = cfg["mcpServers"]["repo-agent-harness"]
    except (OSError, ValueError, KeyError):
        return None
    args = entry.get("args", [])
    trusted = (
        entry.get("command") == "uvx"
        and len(args) == 3
        and args[0] == "--from"
        and bool(_SPEC_RE.match(args[1]))
        and args[2] == "repo-agent-harness-mcp"
    )
    if not trusted:
        return None
    return ["uvx", "--from", args[1], "repo-agent-harness", "hook", event]


def _plugin_bundle_argv(event: str) -> list[str] | None:
    """Resolve the plugin's bundled harness from this script's install location (trusted by construction).

    Invokes the lightweight ``python -m repo_agent_harness.agent_hooks`` entry (~40ms); prefers the
    project venv's python and falls back to ``uv run`` to materialize it cold.
    """
    project = Path(__file__).resolve().parent.parent / "servers" / "harness-mcp"
    if not (project / "pyproject.toml").is_file():
        return None
    module = ["-m", "repo_agent_harness.agent_hooks", event]
    venv_py = project / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return [str(venv_py), *module]
    return ["uv", "run", "--project", str(project), "python", *module]


def run(event: str) -> None:
    """Drive ``event`` through the trusted harness and print its decision JSON (fail-open)."""
    payload = sys.stdin.read()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
        if proc.returncode != 0:
            _empty()
        root = Path(proc.stdout.strip())
        argv = _harness_argv(root, event) or _plugin_bundle_argv(event)
        if argv is None:
            _empty()
        out = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
            cwd=str(root),
        )
        decision = json.loads(out.stdout)
    except Exception:
        _empty()
    print(json.dumps(decision))
    sys.exit(0)
