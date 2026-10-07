#!/usr/bin/env python3
"""PostToolUse shim for Google Antigravity: pipe through trusted harness (fail-open)."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import suppress
from pathlib import Path

_EVENT = "antigravity-post-tool-use"
_GIT_TIMEOUT = 2
_TIMEOUT = 7


def _done() -> None:
    print(json.dumps({}))
    sys.exit(0)


def _bundle_argv() -> list[str] | None:
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
    with suppress(Exception):
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
            check=False,
        )
        root = Path(proc.stdout.strip()) if proc.returncode == 0 else Path.cwd()
        argv = _bundle_argv()
        if argv is None:
            _done()
        try:
            subprocess.run(
                argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
                cwd=str(root),
            )
        except OSError:
            project = Path(__file__).resolve().parent.parent / "servers" / "harness-mcp"
            site_pkgs = list(project.glob(".venv/lib/python*/site-packages"))
            env = dict(subprocess.os.environ)
            paths = [str(project)] + [str(p) for p in site_pkgs]
            env["PYTHONPATH"] = subprocess.os.pathsep.join(paths)
            fb_argv = [sys.executable, "-m", "repo_agent_harness.agent_hooks", _EVENT]
            subprocess.run(
                fb_argv,
                input=payload,
                capture_output=True,
                text=True,
                timeout=_TIMEOUT,
                check=False,
                cwd=str(root),
                env=env,
            )
    _done()


if __name__ == "__main__":
    main()
