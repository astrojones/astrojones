"""End-to-end concurrency regression tests for the harness MCP server.

These guard the reported failure mode — "the server hangs when parallel agents use
it" — by exercising the real FastMCP server (in-memory transport) and the Serena
gateway under concurrent load, with a fake stdio Serena (no LSP, no network). Every
test carries a hard ``timeout`` backstop so a regression fails loudly instead of
hanging the suite.
"""

import asyncio
import sys
from contextlib import suppress
from pathlib import Path

import anyio
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from repo_agent_harness import gateway, health, server

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


class _HangingClose:
    """A client wrapper whose close() blocks forever and reports disconnected (FIX C)."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def is_connected(self) -> bool:
        return False

    async def close(self) -> None:
        await asyncio.sleep(3600)

    async def __aenter__(self):
        return self._inner

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.mark.timeout(45)
async def test_hung_close_does_not_block_siblings(repo, monkeypatch):
    """A hung close() on reconnect must not block the herd waiting on the shared lock.

    With the close bounded by serena_close_timeout(), the first caller's reconnect abandons
    the hung close after the budget and releases the lock; every sibling then proceeds.
    Without the bound the whole gather hangs forever behind the wedged lock.
    """
    monkeypatch.setenv(gateway.SERENA_CLOSE_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    try:
        await gw.call("find_symbol", {"name_path": "seed"})
        gw._client = _HangingClose(gw._client)
        results = await asyncio.gather(*(gw.call("find_symbol", {"name_path": str(i)}) for i in range(8)))
        echoes = sorted(int((r.structuredContent or {}).get("echo")) for r in results)
        assert echoes == list(range(8))
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


# --------------------------------------------------------- in-flight registry under a wedge (#26)


@pytest.mark.timeout(30)
async def test_in_flight_snapshot_lists_a_wedged_call_then_clears(repo, monkeypatch):
    """A live serena_* call wedged in the child shows up in ``in_flight_snapshot`` then clears (#26).

    ``call`` registers every forward in the shared in-flight registry, so while a ``hang`` call is
    blocked in the fake child it is the single, visible entry. A tight ``serena_timeout`` reaps the
    hang shortly after, and the ``finally`` in ``register_inflight`` must drop the entry — proving
    no phantom row leaks after a timed-out (cancelled) call.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "1.0")
    gw = _fake_gateway(repo)
    try:
        # Warm the session so the in-flight entry we observe is the forward, not the cold connect.
        await gw.call("find_symbol", {"name_path": "seed"})
        hang = asyncio.ensure_future(gw.call("hang", {}))

        async def _wait_for_inflight() -> list[dict]:
            for _ in range(200):
                snap = gw.in_flight_snapshot()
                if any(e["tool"] == "serena_hang" for e in snap):
                    return snap
                await asyncio.sleep(0.02)
            return gw.in_flight_snapshot()

        snap = await _wait_for_inflight()
        entry = next(e for e in snap if e["tool"] == "serena_hang")
        assert entry["elapsed_s"] >= 0.0
        assert entry["stalled"] is False

        # The hang call is reaped by serena_timeout; awaiting it surfaces the timeout.
        with pytest.raises(TimeoutError, match="timed out"):
            await hang
        # register_inflight's finally must have removed the entry — no phantom after a cancel/timeout.
        assert all(e["tool"] != "serena_hang" for e in gw.in_flight_snapshot())
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_in_flight_stalled_flag_under_a_wedge(repo, monkeypatch):
    """A long-running in-flight call is flagged ``stalled`` once it crosses the threshold (#26).

    The stall threshold is purely advisory (it never cancels), so dropping ``_INFLIGHT_STALL_SECONDS``
    to zero lets a genuinely in-flight wedged call report ``stalled=True`` deterministically without
    waiting the real 120s.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "1.0")
    monkeypatch.setattr(gateway, "_INFLIGHT_STALL_SECONDS", 0.0)
    gw = _fake_gateway(repo)
    try:
        await gw.call("find_symbol", {"name_path": "seed"})
        hang = asyncio.ensure_future(gw.call("hang", {}))
        stalled = None
        for _ in range(200):
            snap = gw.in_flight_snapshot()
            match = next((e for e in snap if e["tool"] == "serena_hang"), None)
            if match is not None and match["stalled"]:
                stalled = match
                break
            await asyncio.sleep(0.02)
        assert stalled is not None, "wedged call was never flagged stalled"
        assert stalled["elapsed_s"] >= 0.0
        with pytest.raises(TimeoutError, match="timed out"):
            await hang
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_repo_health_surfaces_the_in_flight_wedge(repo, monkeypatch):
    """``health.run`` reports the same wedged in-flight call so diagnostics see it (#26).

    ``_in_flight`` duck-types on ``in_flight_snapshot`` and maps each entry to an ``InFlightCall``,
    so a health snapshot taken while a ``hang`` forward is in flight must list it. The snapshot is
    computed in a worker thread (as the real health check is) to mirror the cross-thread read.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "1.0")
    gw = _fake_gateway(repo)
    try:
        await gw.call("find_symbol", {"name_path": "seed"})
        hang = asyncio.ensure_future(gw.call("hang", {}))
        snapshot = None
        for _ in range(200):
            snap = await anyio.to_thread.run_sync(
                lambda: health.run(str(repo), only="diagnostics", refresh=True, gateway=gw)
            )
            if any(c.tool == "serena_hang" for c in snap.in_flight):
                snapshot = snap
                break
            await asyncio.sleep(0.02)
        assert snapshot is not None, "health snapshot never surfaced the in-flight wedge"
        call = next(c for c in snapshot.in_flight if c.tool == "serena_hang")
        assert call.cwd == (gw.root or "")
        assert call.elapsed_s >= 0.0
        with pytest.raises(TimeoutError, match="timed out"):
            await hang
    finally:
        await gw.aclose()


# ----------------------------------------------- actionable connect/lock-wedge message (#25)


class _HangingConnect:
    """A client whose connect (``__aenter__``) blocks forever — to wedge the gateway connect."""

    def is_connected(self) -> bool:
        return False

    async def __aenter__(self):
        await anyio.sleep_forever()

    async def __aexit__(self, *exc) -> bool:
        return False

    async def close(self) -> None:
        return None


@pytest.mark.timeout(30)
async def test_wedged_connect_raises_actionable_message(repo, monkeypatch):
    """A wedged connect surfaces a TimeoutError naming the tool + connect/dispatch/lock (#25).

    The outer dispatch deadline bounds the whole ``call`` (connect + lock-wait), so a connect that
    never returns is reaped with a message an operator can act on: it must mention the offending
    tool and point at the dispatch/connect/lock as the wedge site, not a bare 'timed out'.
    """
    monkeypatch.setenv(gateway.SERENA_DISPATCH_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    monkeypatch.setattr(gw, "_ensure_client", _HangingConnect)
    try:
        with pytest.raises(TimeoutError) as excinfo:
            await gw.call("find_symbol", {"name_path": "x"})
        message = str(excinfo.value)
        assert "find_symbol" in message
        assert any(word in message for word in ("dispatch", "connect", "lock")), message
    finally:
        with suppress(BaseException):
            await gw.aclose()
