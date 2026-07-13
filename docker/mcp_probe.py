#!/usr/bin/env python3
"""Boot the bundled MCP server over stdio and assert repo_context_overview is registered.

Drives a minimal JSON-RPC handshake (initialize -> notifications/initialized -> tools/list)
against `uv run repo-agent-harness-mcp` and checks the tool list. Exit 0 on success.
This proves the server boots and exposes its tools in a vanilla environment — without
needing the model or an API key.
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

_HARNESS = sys.argv[1] if len(sys.argv) > 1 else "/plugins/astrojones/servers/harness-mcp"
_WANT = "repo_context_overview"


def main() -> int:
    proc = subprocess.Popen(
        ["uv", "run", "--project", _HARNESS, "repo-agent-harness-mcp"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        cwd="/workspace/repo",
    )
    try:
        def send(obj: dict) -> None:
            assert proc.stdin is not None
            proc.stdin.write(json.dumps(obj) + "\n")
            proc.stdin.flush()

        def read_until(rid: int, timeout: float = 15.0) -> dict | None:
            assert proc.stdout is not None
            end = time.time() + timeout
            while time.time() < end:
                line = proc.stdout.readline()
                if not line:
                    return None
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue  # server banner / log line — skip
                if obj.get("id") == rid:
                    return obj
            return None

        send({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "mcp-probe", "version": "1"},
            },
        })
        if read_until(1) is None:
            print("  FAIL: no initialize response")
            return 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        resp = read_until(2)
        if resp is None:
            print("  FAIL: no tools/list response")
            return 1
        tools = resp.get("result", {}).get("tools", [])
        names = [t.get("name", "") for t in tools]
        print(f"  tools ({len(names)}): {', '.join(names)}")
        return 0 if _WANT in names else 1
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()


if __name__ == "__main__":
    raise SystemExit(main())