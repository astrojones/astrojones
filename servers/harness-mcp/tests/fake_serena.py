"""A tiny fake Serena MCP server (stdio) for gateway tests — no LSP, no network."""

import os

from fastmcp import FastMCP

mcp = FastMCP("fake-serena")


@mcp.tool()
def find_symbol(name_path: str) -> dict:
    """Echo the requested symbol path."""
    return {"echo": name_path}


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


if __name__ == "__main__":
    mcp.run()
