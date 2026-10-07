#!/usr/bin/env python3
"""PreToolUse shim: pipe the event through a *trusted* harness, never a repo-supplied one.

The policy logic lives in the repo-agent-harness package (one brain); this shim only resolves
*which* harness to ask, in trust order:

1. The repo's own `.mcp.json` entry, **only if it is the canonical sha-pinned uvx form** — an
   explicit, immutable pin (used by opencode/CI or `harness-init --pin`).
2. Otherwise, the plugin's **bundled** harness, resolved from this script's own install location
   (`<plugin_root>/servers/harness-mcp`) — trusted because it is the installed plugin's own code,
   the same harness it runs as the MCP server, and is NOT controlled by the target repo.

This keeps the guard active in a default zero-footprint install (no repo `.mcp.json` required),
while still refusing to execute any repo-supplied command shape: a no-approval hook must never run
arbitrary code a repo could plant.

Fail-open by contract: any error (no git repo, no trusted harness, cold-cache timeout) prints an
empty response and exits 0 — a hook problem never blocks work.
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
    r"^git\+https://github\.com/astrojones/astrojones"
    r"@[0-9a-f]{40}#subdirectory=servers/harness-mcp$"
)


def _allow() -> None:
    print(json.dumps({}))
    sys.exit(0)


def _harness_argv(root: Path) -> list[str] | None:
    """Resolve the repo's own sha-pinned harness from its `.mcp.json`, if trusted.

    Only the canonical sha-pinned uvx form is honoured; any other shape (or a missing/invalid
    `.mcp.json`) returns None so the caller falls back to the plugin's bundled harness.
    """
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
    return ["uvx", "--from", args[1], "repo-agent-harness", "hook", _EVENT]


def _plugin_bundle_argv() -> list[str] | None:
    """Resolve the plugin's own bundled harness from this script's install location.

    The hook ships at ``<plugin_root>/hooks/pre_tool_use.py``, so the bundled harness lives at
    ``<plugin_root>/servers/harness-mcp``. This path is trusted *by construction*: it is resolved
    from the script's own location (the installed plugin the user chose to install), NOT from
    anything the target repo controls — so a malicious repo cannot redirect it. It is the same
    harness the plugin already runs as its MCP server (see run-mcp.sh). Returns None when not
    running from a plugin layout (e.g. the script invoked standalone).

    Invokes the lightweight ``python -m repo_agent_harness.agent_hooks`` entry (imports only the
    hook module, ~40ms) rather than the ``repo-agent-harness`` console script (full CLI import,
    ~600ms) — this runs on every guarded tool call. Prefers the project venv's python directly
    (created once the MCP server has run) and falls back to ``uv run`` to materialize it cold.
    """
    project = Path(__file__).resolve().parent.parent / "servers" / "harness-mcp"
    if not (project / "pyproject.toml").is_file():
        return None
    module = ["-m", "repo_agent_harness.agent_hooks", _EVENT]
    venv_py = project / ".venv" / "bin" / "python"
    if venv_py.is_file():
        return [str(venv_py), *module]
    return ["uv", "run", "--project", str(project), "python", *module]


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
        argv = _harness_argv(root) or _plugin_bundle_argv()
        if argv is None:
            _allow()
        try:
            out = subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
                cwd=str(root),  # the pinned harness reads agent/policies/ from its cwd repo
            )
        except OSError:
            project = Path(__file__).resolve().parent.parent / "servers" / "harness-mcp"
            site_pkgs = list(project.glob(".venv/lib/python*/site-packages"))
            env = dict(subprocess.os.environ)
            paths = [str(project)] + [str(p) for p in site_pkgs]
            env["PYTHONPATH"] = subprocess.os.pathsep.join(paths)
            fb_argv = [sys.executable, "-m", "repo_agent_harness.agent_hooks", _EVENT]
            out = subprocess.run(
                fb_argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
                cwd=str(root),
                env=env,
            )
        decision = json.loads(out.stdout)
    except Exception:
        _allow()
    print(json.dumps(decision))
    sys.exit(0)


if __name__ == "__main__":
    main()
