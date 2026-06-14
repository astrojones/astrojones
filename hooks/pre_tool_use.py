#!/usr/bin/env python3
"""PreToolUse shim: pipe the event through the repo's own pinned harness.

The policy logic lives in the repo-agent-harness package (one brain); this shim
only resolves *which* harness to ask: it reads the `repo-agent-harness` server
entry from the current repo's `.mcp.json` and, **only if it is the canonical
sha-pinned uvx form**, re-runs it with the console script swapped from
`repo-agent-harness-mcp` to `repo-agent-harness hook pre-tool-use`. Hooks run
with no user-approval gate, so any other shape (including the harness repo's
own relative-path dogfood form) is never executed and fails open instead.

Fail-open by contract: any error (no repo, no .mcp.json, uvx cold-cache timeout)
prints an empty response and exits 0 — a hook problem never blocks work.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

_EVENT = "pre-tool-use"
_GIT_TIMEOUT = 2
_TIMEOUT = 7  # with git's 2s this stays under the 10s budget in hooks.json
_SPEC_RE = re.compile(
    r"^git\+https://github\.com/astrojones/raisl"
    r"@[0-9a-f]{40}#subdirectory=servers/harness-mcp$"
)


def _allow() -> None:
    print(json.dumps({}))
    sys.exit(0)


def _harness_argv(root: Path) -> list[str] | None:
    cfg = json.loads((root / ".mcp.json").read_text())
    entry = cfg["mcpServers"]["repo-agent-harness"]
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
    return ["uvx", "--from", args[1], "repo-agent-harness", "hook", _EVENT]


def main() -> None:
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
            cwd=str(root),  # the pinned harness reads agent/policies/ from its cwd repo
        )
        decision = json.loads(out.stdout)
    except Exception:
        _allow()
    print(json.dumps(decision))
    sys.exit(0)


if __name__ == "__main__":
    main()
