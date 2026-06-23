"""End-to-end concurrency regression tests for the harness MCP server.

These guard the reported failure mode — "the server hangs when parallel agents use
it" — by exercising the real FastMCP server (in-memory transport) and the Serena
gateway under concurrent load, with a fake stdio Serena (no LSP, no network). Every
test carries a hard ``timeout`` backstop so a regression fails loudly instead of
hanging the suite.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from repo_agent_harness import gateway, server

pytestmark = pytest.mark.anyio

FAKE = Path(__file__).parent / "fake_serena.py"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fake_gateway(repo: Path) -> gateway.SerenaGateway:
    transport = StdioTransport(command=sys.executable, args=[str(FAKE)], cwd=str(repo), keep_alive=True)
    return gateway.SerenaGateway(str(repo), transport=transport)


# --------------------------------------------------------------- gateway-level races


@pytest.mark.timeout(30)
async def test_finite_late_response_does_not_corrupt_next_call(repo, monkeypatch):
    """A finite Serena reply that lands *after* the timeout must not bleed into the next call."""
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.3")
    gw = _fake_gateway(repo)
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            await gw.call("slow", {"seconds": 1.0, "marker": "STALE"})
        # The stale "STALE" reply arrives ~0.7s later; the next call must get its own answer.
        result = await gw.call("find_symbol", {"name_path": "fresh"})
        assert (result.structuredContent or {}).get("echo") == "fresh"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_many_concurrent_gateway_calls_all_resolve(repo):
    """Concurrent forwards over one gateway each get their own correct response."""
    gw = _fake_gateway(repo)
    try:
        results = await asyncio.gather(*(gw.call("find_symbol", {"name_path": str(i)}) for i in range(12)))
        echoes = sorted(int((r.structuredContent or {}).get("echo")) for r in results)
        assert echoes == list(range(12))
    finally:
        await gw.aclose()


# ------------------------------------------------------------------- server-level e2e


@pytest.fixture
def server_with_fake_serena(repo, monkeypatch):
    """Run the real MCP server in the fixture repo with Serena redirected to the fake child.

    The proxied ``serena_*`` tools share the module-global gateway, so pointing that
    gateway at an injected transport reroutes them to the fake without touching real Serena.
    """
    monkeypatch.chdir(repo)
    transport = StdioTransport(command=sys.executable, args=[str(FAKE)], cwd=str(repo), keep_alive=True)
    monkeypatch.setattr(server._serena, "_injected_transport", transport)
    monkeypatch.setattr(server._serena, "_client", None)
    return server


@pytest.mark.timeout(45)
async def test_hung_serena_does_not_block_other_tools(server_with_fake_serena, monkeypatch):
    """The headline regression: a hung serena_* call times out and does NOT freeze other tools."""
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "1.0")
    srv = server_with_fake_serena
    try:
        async with Client(srv.mcp) as client:
            outcomes = await asyncio.gather(
                client.call_tool("serena_hang", {}),
                *(client.call_tool("repo_context_status", {}) for _ in range(6)),
                return_exceptions=True,
            )
        hung, repo_calls = outcomes[0], outcomes[1:]
        # The hung serena call surfaces an error (timeout), not a hang.
        assert isinstance(hung, Exception) or getattr(hung, "is_error", False)
        # Every repo_* call completed normally — they were not serialized behind the hang.
        assert all(not isinstance(r, Exception) for r in repo_calls)
        assert all(r.data.get("branch") for r in repo_calls)
    finally:
        await srv._serena.aclose()


@pytest.mark.timeout(45)
async def test_parallel_mixed_load_completes(server_with_fake_serena):
    """A burst of mixed concurrent tool calls all complete — no threadpool/event-loop deadlock."""
    srv = server_with_fake_serena
    try:
        async with Client(srv.mcp) as client:
            calls = []
            for i in range(8):
                calls.extend(
                    (
                        client.call_tool("repo_context_status", {}),
                        client.call_tool("repo_search_files", {"pattern": "*.py", "limit": 5}),
                        client.call_tool("serena_find_symbol", {"name_path": f"sym{i}"}),
                    )
                )
            results = await asyncio.gather(*calls, return_exceptions=True)
        errors = [repr(r) for r in results if isinstance(r, Exception)]
        assert not errors, errors
        assert len(results) == 24
    finally:
        await srv._serena.aclose()


@pytest.mark.timeout(30)
async def test_server_lifespan_pre_warms_serena(server_with_fake_serena):
    """The server lifespan starts Serena in the background so the first serena_* call is warm."""
    srv = server_with_fake_serena
    try:
        async with Client(srv.mcp) as client:
            # Wait for the background warmup connect to finish; if it has not started yet
            # the in-flight task is still exposed on the gateway.
            task = srv._serena._connect_task
            if task is not None and not task.done():
                await task
            assert srv._serena._client is not None, "Serena session was not warmed during lifespan"
            assert srv._serena._client.is_connected()
            result = await client.call_tool("serena_find_symbol", {"name_path": "warm"})
            assert getattr(result, "is_error", False) is False
            assert (result.structured_content or {}).get("echo") == "warm"
    finally:
        await srv._serena.aclose()
