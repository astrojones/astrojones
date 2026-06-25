"""Acceptance gate for the tool-dispatch timeout middleware (issue #23).

These drive the *real* FastMCP middleware path — every tool call funnels through
``ToolTimeoutMiddleware.on_call_tool`` — to prove a runaway tool dispatch is reaped at
``gateway.tool_timeout()`` instead of hanging the host, and that a sibling call stays
responsive while/after a slow one is timing out. They use the in-memory ``Client``
transport against the fixture repo with Serena redirected to the fake child (no LSP,
no network), so they are deterministic Tier-1 tests.

Every test carries a hard ``pytest.mark.timeout`` backstop strictly larger than the
in-test ``tool_timeout`` budget, so a *regression* (the middleware reverted / not firing)
FAILS the test on the timeout marker rather than hanging the whole suite.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.tools import Tool
from repo_agent_harness import gateway, server

pytestmark = pytest.mark.anyio

FAKE = Path(__file__).parent / "fake_serena.py"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def server_with_fake_serena(repo, monkeypatch):
    """Real MCP server in the fixture repo with Serena redirected to the fake child.

    Mirrors the fixture in test_concurrency.py: the proxied ``serena_*`` tools share the
    module-global gateway, so pointing that gateway at an injected transport reroutes them
    to the fake without touching real Serena. Generic ``@mcp.tool`` handlers go straight
    through the middleware, which is what these tests exercise.
    """
    monkeypatch.chdir(repo)
    transport = StdioTransport(command=sys.executable, args=[str(FAKE)], cwd=str(repo), keep_alive=True)
    monkeypatch.setattr(server._serena, "_injected_transport", transport)
    monkeypatch.setattr(server._serena, "_client", None)
    return server


def _add_slow_tool(srv, name: str, seconds: float) -> None:
    """Register a generic async tool that simply sleeps — a runaway handler with no gateway deadline."""

    async def _slow() -> str:
        await asyncio.sleep(seconds)
        return "done"

    srv.mcp.local_provider.add_tool(Tool.from_function(_slow, name=name))


@pytest.mark.timeout(20)
async def test_slow_tool_dispatch_is_reaped_at_tool_timeout(server_with_fake_serena, monkeypatch):
    """A generic tool that runs past ``tool_timeout`` surfaces an error within the deadline (#23).

    The middleware bounds every dispatch with ``anyio.fail_after(gateway.tool_timeout())`` and
    maps the resulting cancellation to ``ToolTimeoutError``. With the budget set well below the
    handler's own sleep, the call must return an ``is_error`` ToolResult (FastMCP maps the raised
    error) naming the tool — promptly, not after the 20s pytest-timeout backstop.
    """
    monkeypatch.setenv(gateway.TOOL_TIMEOUT_ENV, "0.5")
    srv = server_with_fake_serena
    try:
        _add_slow_tool(srv, "slow_runaway", seconds=3600.0)
        async with Client(srv.mcp) as client:
            result = await client.call_tool("slow_runaway", {}, raise_on_error=False)
        assert getattr(result, "is_error", False) is True
        text = " ".join(getattr(c, "text", "") for c in result.content)
        assert "slow_runaway" in text
    finally:
        srv.mcp.local_provider.remove_tool("slow_runaway")
        await srv._serena.aclose()


@pytest.mark.timeout(20)
async def test_sibling_tool_stays_responsive_during_slow_dispatch(server_with_fake_serena, monkeypatch):
    """A slow dispatch being reaped must not serialize/starve a concurrent sibling call (#23).

    Fired together: the runaway handler (reaped at ``tool_timeout``) and a fast generic tool.
    The sibling must complete normally and not be queued behind the slow one — proving the host
    heartbeat is never starved by a wedged handler.
    """
    monkeypatch.setenv(gateway.TOOL_TIMEOUT_ENV, "0.5")
    srv = server_with_fake_serena
    try:
        _add_slow_tool(srv, "slow_runaway", seconds=3600.0)
        async with Client(srv.mcp) as client:
            slow, fast = await asyncio.gather(
                client.call_tool("slow_runaway", {}, raise_on_error=False),
                client.call_tool("repo_context_status", {}, raise_on_error=False),
                return_exceptions=True,
            )
        # The slow call surfaced an error (timeout), the fast sibling resolved normally.
        assert isinstance(slow, Exception) or getattr(slow, "is_error", False)
        assert not isinstance(fast, Exception)
        assert getattr(fast, "is_error", False) is False
        assert fast.data.get("branch")
    finally:
        srv.mcp.local_provider.remove_tool("slow_runaway")
        await srv._serena.aclose()


@pytest.mark.timeout(20)
async def test_responsive_after_slow_dispatch_timed_out(server_with_fake_serena, monkeypatch):
    """After a runaway dispatch is reaped, the next call resolves promptly — no lingering wedge (#23)."""
    monkeypatch.setenv(gateway.TOOL_TIMEOUT_ENV, "0.5")
    srv = server_with_fake_serena
    try:
        _add_slow_tool(srv, "slow_runaway", seconds=3600.0)
        async with Client(srv.mcp) as client:
            timed_out = await client.call_tool("slow_runaway", {}, raise_on_error=False)
            assert getattr(timed_out, "is_error", False) is True
            after = await client.call_tool("repo_context_status", {}, raise_on_error=False)
            assert getattr(after, "is_error", False) is False
            assert after.data.get("branch")
    finally:
        srv.mcp.local_provider.remove_tool("slow_runaway")
        await srv._serena.aclose()
