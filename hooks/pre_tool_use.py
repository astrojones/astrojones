#!/usr/bin/env python3
"""PreToolUse shim: pipe the event through the repo's own pinned harness.

The policy logic lives in the repo-agent-harness package (one brain); this shim
only resolves *which* harness to ask: it reads the `repo-agent-harness` server
entry from the current repo's `.mcp.json` and re-runs the same command with the
console script swapped from `repo-agent-harness-mcp` to `repo-agent-harness hook
pre-tool-use`. Repos without a harness entry are allowed through untouched.

Fail-open by contract: any error (no repo, no .mcp.json, uvx cold-cache timeout)
prints an empty response and exits 0 — a hook problem never blocks work.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

_EVENT = "pre-tool-use"
_TIMEOUT = (
    8  # hooks.json allows 10s; first-ever uvx resolution may exceed this and fail open
)


def _allow() -> None:
    print(json.dumps({}))
    sys.exit(0)


def _harness_argv(root: Path) -> list[str] | None:
    cfg = json.loads((root / ".mcp.json").read_text())
    entry = cfg["mcpServers"]["repo-agent-harness"]
    argv = [entry["command"], *entry.get("args", [])]
    if argv[-1] != "repo-agent-harness-mcp":
        return None
    return [*argv[:-1], "repo-agent-harness", "hook", _EVENT]


def main() -> None:
    payload = sys.stdin.read()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode != 0:
            _allow()
        root = Path(proc.stdout.strip())
        argv = _harness_argv(root)
        if argv is None:
            _allow()
        out = subprocess.run(
            argv,
            input=payload,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT,
            check=False,
            cwd=str(root),  # dogfood .mcp.json uses a relative --project path
        )
        decision = json.loads(out.stdout)
    except Exception:
        _allow()
    print(json.dumps(decision))
    sys.exit(0)


if __name__ == "__main__":
    main()
