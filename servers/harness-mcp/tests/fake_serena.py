"""A tiny fake Serena MCP server for gateway tests — no LSP.

Default transport is stdio (the child-process tests). Invoked with Serena's real daemon argv
(``start-mcp-server ... --transport streamable-http --port N --project X``) it instead serves
streamable-http like the real daemon — including ``get_current_config`` reporting the active
project, which is the harness's daemon identity check.
"""

import os
import sys
import time
from pathlib import Path

from fastmcp import FastMCP

mcp = FastMCP("Serena")

_PROJECT = os.environ.get("FAKE_SERENA_PROJECT", "")


@mcp.tool()
def initial_instructions() -> str:
    """Mirror Serena's instructions — including the line the daemon identity check parses."""
    return f"You are a fake Serena.\nThe project with name '{Path(_PROJECT).name}' at {_PROJECT} is activated.\n"


@mcp.tool()
def find_symbol(name_path: str) -> dict:
    """Echo the requested symbol path (``result`` mirrors the real Serena output contract)."""
    if _wedged:
        time.sleep(3600)
    return {"echo": name_path, "result": name_path}


@mcp.tool()
def find_implementations(name_path: str, relative_path: str) -> dict:
    """Echo the requested symbol path (exercises the capable-language forward path in tests)."""
    return {"echo": name_path, "result": name_path}


@mcp.tool()
def get_diagnostics_for_file(relative_path: str) -> dict:
    """Return canned grouped diagnostics: 1 error, 2 warnings."""
    return {
        relative_path: {
            "ERROR": {"<file>": [{"message": "something is wrong"}]},
            "WARNING": {"<file>": [{"message": "w1"}, {"message": "w2"}]},
        }
    }


@mcp.tool()
def boom() -> str:
    """Always fail with a tool error."""
    msg = "kaboom"
    raise ValueError(msg)


@mcp.tool()
def crash() -> str:
    """Kill the server process mid-flight."""
    os._exit(1)


@mcp.tool()
def hang() -> str:
    """Block forever — used to test the gateway call timeout."""
    time.sleep(3600)
    return "never"


@mcp.tool()
def slow(seconds: float, marker: str = "slow") -> dict:
    """Sleep a finite ``seconds`` then echo ``marker`` (finite late-response test)."""
    if _wedged:
        time.sleep(3600)
    time.sleep(seconds)
    return {"echo": marker}


_wedged = False


@mcp.tool()
def wedge() -> str:
    """Wedge this child: every later ``find_symbol``/``slow`` blocks forever (a hung LSP).

    The flag is process-global, so the gateway can only escape by respawning a fresh child —
    exactly what the wedge-recovery test asserts after ``_WEDGE_TIMEOUTS`` consecutive timeouts.
    """
    global _wedged  # noqa: PLW0603 — a process-global flag is the point: only a respawn clears it
    _wedged = True
    return "wedged"


def _flag(argv: list[str], name: str) -> str | None:
    """The value following ``name`` in ``argv``, or None."""
    if name in argv:
        idx = argv.index(name)
        if idx + 1 < len(argv):
            return argv[idx + 1]
    return None


if __name__ == "__main__":
    # Optional cold-boot delay: sleeping before run() defers the MCP initialize handshake, so
    # the gateway's connect blocks long enough to be cancelled mid-flight (connect-storm test).
    _boot_delay = float(os.environ.get("FAKE_SERENA_BOOT_DELAY", "0") or "0")
    if _boot_delay:
        time.sleep(_boot_delay)
    argv = sys.argv[1:]
    project = _flag(argv, "--project")
    if project and not _PROJECT:
        _PROJECT = str(Path(project).resolve())
    if _flag(argv, "--transport") == "streamable-http":
        mcp.run(transport="http", host=_flag(argv, "--host") or "127.0.0.1", port=int(_flag(argv, "--port") or "8000"))
    else:
        mcp.run()
