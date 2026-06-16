"""A tiny fake Serena MCP server (stdio) for gateway tests — no LSP, no network."""

import os
import time

from fastmcp import FastMCP

mcp = FastMCP("fake-serena")


@mcp.tool()
def find_symbol(name_path: str) -> dict:
    """Echo the requested symbol path (``result`` mirrors the real Serena output contract)."""
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
    time.sleep(seconds)
    return {"echo": marker}


if __name__ == "__main__":
    mcp.run()
