"""Stress / regression tests for the shared Serena session under parallel-subagent load.

The reported flakiness — "the plugin is flaky when several parallel subagents use Serena,
especially with TypeScript" — has a concrete suspect in :mod:`repo_agent_harness.gateway`:
all subagents in a Claude Code session route through ONE harness process, ONE
``SerenaGateway``, ONE persistent MCP session, ONE child Serena. ``SerenaGateway.call``
invalidates that *shared* session (closing the child + every language server) on ANY
single call's timeout or exception. With concurrent callers, one slow/aborted call tears
the session out from under its siblings — exactly an intermittent, load-dependent failure.

These tests force that interleaving *deterministically* (a flaky test for flakiness is
useless as a release gate) using the LSP-free fake Serena, so the race is exercised by
construction, not by luck. They are expected to FAIL on a gateway that tears the shared
session down on a single call's failure, and to pass once teardown is scoped to genuine
transport death. Each test carries a hard ``timeout`` so a regression fails loudly.
"""

import asyncio
import os
import sys
from pathlib import Path

import anyio
import psutil
import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from repo_agent_harness import gateway

pytestmark = pytest.mark.anyio

FAKE = Path(__file__).parent / "fake_serena.py"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fake_gateway(repo: Path) -> gateway.SerenaGateway:
    """A gateway whose child is the LSP-free fake Serena (stdio, kept alive across calls)."""
    transport = StdioTransport(command=sys.executable, args=[str(FAKE)], cwd=str(repo), keep_alive=True)
    return gateway.SerenaGateway(str(repo), transport=transport)


async def _echo(result) -> str | None:
    """Pull the echoed marker out of a fake-Serena CallToolResult."""
    return (result.structuredContent or {}).get("echo")


@pytest.mark.timeout(30)
async def test_sibling_survives_a_concurrent_calls_timeout(repo, monkeypatch):
    """A sibling serena call must survive a *concurrent* call timing out on the shared session.

    Timeline (per-call timeout = 0.5s, one shared healthy child):
      t=0.00  victim starts ``hang`` (blocks forever)
      t=0.30  sibling starts ``slow(0.3)`` — its own deadline is t=0.80
      t=0.50  victim hits its timeout → gateway invalidates → closes the SHARED child
      t=0.60  sibling's reply was due — but the child it was talking to is gone

    On the current gateway the sibling dies with a transport error because the timeout of an
    *unrelated* call killed the child it shared. The healthy child should have answered it.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    try:
        # Warm the session so victim and sibling provably share one already-open child.
        await gw.call("find_symbol", {"name_path": "warm"})

        async def victim() -> None:
            with pytest.raises(TimeoutError, match="timed out"):
                await gw.call("hang", {})

        async def sibling():
            await asyncio.sleep(0.3)  # still in flight when the victim tears down at 0.5s
            return await gw.call("slow", {"seconds": 0.3, "marker": "alive"})

        _, result = await asyncio.gather(victim(), sibling())
        assert await _echo(result) == "alive", "a concurrent call's timeout killed the shared session"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_sibling_survives_a_concurrent_calls_cancellation(repo, monkeypatch):
    """A sibling serena call must survive a *concurrent* call being cancelled mid-flight.

    Cancellation is the most frequent real-world trigger: a subagent finishes (or the
    orchestrator cancels it) while its serena call is in flight. The original gateway tore the
    shared session down on *any* exception — cancellation included — killing the healthy child
    out from under siblings. The child is fine; only one *caller* went away, so the gateway must
    leave the session intact and the sibling must still get its answer.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "30")  # high: not exercising timeout here
    gw = _fake_gateway(repo)
    try:
        await gw.call("find_symbol", {"name_path": "warm"})

        sibling_task = asyncio.create_task(gw.call("slow", {"seconds": 0.6, "marker": "alive"}))
        victim_task = asyncio.create_task(gw.call("slow", {"seconds": 5.0, "marker": "victim"}))
        await asyncio.sleep(0.2)  # both provably in flight on the shared child
        victim_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim_task

        result = await sibling_task
        assert await _echo(result) == "alive", "a concurrent call's cancellation killed the shared session"
    finally:
        await gw.aclose()


@pytest.mark.timeout(45)
async def test_wedged_child_recovers_after_consecutive_timeouts(repo, monkeypatch):
    """A child wedged so that EVERY call times out is respawned — recovery is not lost.

    The dangerous half of the teardown fix: declining to close the shared child on a *single*
    timeout must not also remove recovery from a genuinely hung language server — a real
    ``tsserver`` failure mode where the connection stays open but requests are never answered
    (so ``is_connected()`` keeps reporting it alive). After ``_WEDGE_TIMEOUTS`` consecutive
    timeouts with no success the gateway must reap and respawn; the fresh child (a new process
    with the wedge flag unset) answers again. Without the consecutive-timeout counter this
    would time out forever — strictly worse than the flakiness being fixed.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    try:
        assert await _echo(await gw.call("find_symbol", {"name_path": "warm"})) == "warm"
        await gw.call("wedge", {})  # this child now hangs on every find_symbol/slow

        for _ in range(gateway._WEDGE_TIMEOUTS):
            with pytest.raises(TimeoutError):
                await gw.call("find_symbol", {"name_path": "wedged"})

        # The consecutive timeouts reaped the wedged child; the next call respawns a fresh one.
        result = await gw.call("find_symbol", {"name_path": "recovered"})
        assert await _echo(result) == "recovered", "wedged child was never respawned — recovery lost"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_mid_request_wedge_is_hard_killed(repo, monkeypatch):
    """A child whose stdio read ignores the cooperative timeout is hard-killed on the hard deadline.

    Reproduces the issue #30 production defect: when the child wedges *mid-request* the fastmcp
    stdio read never observes the ``anyio.fail_after`` cancellation, so the cooperative per-call
    timeout cannot unwind the await and the agent hangs forever — the ``_reap_if_wedged`` recovery
    is unreachable. The read is made cancellation-deaf here by an inner shielded cancel scope,
    exactly that failure mode. The out-of-band watchdog must fire at ``serena_hard_deadline()``
    (= per-call timeout + grace), force-kill the child's *process group* from the outside —
    closing its stdout so the blocked read finally errors out — and let the gateway reap and
    respawn. Without the watchdog the call hangs and the ``@pytest.mark.timeout`` fails it.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.5")
    monkeypatch.setenv(gateway.SERENA_HARD_DEADLINE_ENV, "0.5")  # hard deadline = 0.5 + 0.5 = 1.0s
    gw = _fake_gateway(repo)
    try:
        # Warm the shared child and capture the PID the gateway discovered out-of-band at connect.
        assert await _echo(await gw.call("find_symbol", {"name_path": "warm"})) == "warm"
        pid = gw._child_pid
        assert pid is not None and psutil.pid_exists(pid), "child PID was not captured at connect"

        # Make the forwarded read deaf to cancellation: an inner shielded scope swallows the 0.5s
        # cooperative timeout, so only an external kill can free the await — the production wedge.
        real_call = Client.call_tool_mcp

        async def cancellation_deaf(self, name, arguments):
            with anyio.CancelScope(shield=True):
                return await real_call(self, name, arguments)

        monkeypatch.setattr(Client, "call_tool_mcp", cancellation_deaf)

        await gw.call("wedge", {})  # flips the child's wedge flag (this call still returns fast)
        with pytest.raises(TimeoutError):
            # find_symbol now blocks forever in the child; the shielded read ignores fail_after;
            # the watchdog hard-kills at 1.0s, the read errors out, dispatch unwinds as TimeoutError.
            await gw.call("find_symbol", {"name_path": "wedged"})

        assert not psutil.pid_exists(pid), "watchdog did not hard-kill the wedged child's process group"

        # A cancellable read again: the gateway must respawn a fresh, healthy child and answer.
        monkeypatch.setattr(Client, "call_tool_mcp", real_call)
        assert await _echo(await gw.call("find_symbol", {"name_path": "recovered"})) == "recovered"
    finally:
        await gw.aclose()


@pytest.mark.timeout(40)
async def test_connect_time_wedge_is_hard_killed(repo, monkeypatch):
    """A child that wedges *mid-connect* (cancellation-deaf initialize read) is hard-killed.

    The dispatch watchdog (issue #30) only arms once the session is connected, so a child that
    wedges inside ``__aenter__`` itself — the very first call's cold connect, where no PID is
    captured yet — was still an unbounded hang (the live ``serena_initial_instructions`` stall).
    ``FAKE_SERENA_BOOT_DELAY`` makes the child spawn then sleep *before* the MCP initialize
    handshake, so the connect read blocks while the child process is alive and killable; shielding
    ``__aenter__`` makes that read deaf to the cooperative connect ``fail_after`` exactly like the
    production wedge. Only the connect watchdog's external kill can free it: it must discover the
    freshly spawned child, kill its process group, surface a ``TimeoutError`` (never hang), and let
    a fresh connect succeed afterwards.

    The await is *message-matched*, not bounded by a plain ``wait_for`` deadline: a pre-fix gateway
    hangs, so ``wait_for`` would itself raise a bare ``TimeoutError`` and mask the regression — the
    assertion must see the watchdog's own "hard-killed" error, not the test harness's own timeout.
    """
    monkeypatch.setenv(gateway.SERENA_CONNECT_TIMEOUT_ENV, "4.0")
    monkeypatch.setenv(gateway.SERENA_HARD_DEADLINE_ENV, "0.5")  # connect hard deadline = 4.0 + 0.5

    # Make the connect read deaf to cancellation: the shielded scope swallows the cooperative
    # connect timeout, so only the watchdog's external kill can unwedge the blocked __aenter__.
    real_aenter = Client.__aenter__

    async def cancellation_deaf(self):
        with anyio.CancelScope(shield=True):
            return await real_aenter(self)

    monkeypatch.setattr(Client, "__aenter__", cancellation_deaf)

    # The child sleeps before the initialize handshake (the env must be passed explicitly — mcp's
    # stdio client does not forward the parent env), so the connect blocks on a live, killable child.
    wedged = StdioTransport(
        command=sys.executable,
        args=[str(FAKE)],
        cwd=str(repo),
        keep_alive=True,
        env={**os.environ, "FAKE_SERENA_BOOT_DELAY": "3600"},
    )
    gw = gateway.SerenaGateway(str(repo), transport=wedged)
    child_pid = None
    try:
        before = {child.pid for child in psutil.Process().children()}
        call_task = asyncio.create_task(gw.call("find_symbol", {"name_path": "x"}))

        # Wait for the wedged child to spawn so we can prove the watchdog later kills it.
        for _ in range(400):
            new = {child.pid for child in psutil.Process().children()} - before
            if new:
                child_pid = min(new)
                break
            await asyncio.sleep(0.01)
        assert child_pid is not None and psutil.pid_exists(child_pid), "wedged child never spawned"

        # The connect watchdog must fire at the connect hard deadline and surface its own timeout
        # (matching its message, not the harness's wait_for, so a pre-fix hang fails loudly).
        with pytest.raises(TimeoutError, match="hard-killed"):
            await asyncio.wait_for(call_task, timeout=20)
        assert not psutil.pid_exists(child_pid), "connect watchdog did not hard-kill the mid-connect child"

        # Recovery: a cancellable connect to a child with no boot delay must respawn and answer.
        monkeypatch.setattr(Client, "__aenter__", real_aenter)
        gw._injected_transport = StdioTransport(
            command=sys.executable, args=[str(FAKE)], cwd=str(repo), keep_alive=True
        )
        assert await _echo(await gw.call("find_symbol", {"name_path": "recovered"})) == "recovered"
    finally:
        if child_pid is not None:
            gateway._kill_child_group(child_pid)
        await gw.aclose()


@pytest.mark.timeout(90)
async def test_intermittent_timeouts_do_not_disturb_concurrent_healthy_calls(repo, monkeypatch):
    """Timeouts interleaved with healthy concurrent calls disturb neither, across many rounds.

    Post-fix a timeout no longer tears the shared session down, so this guards two things at
    once: (1) a timed-out call never breaks the healthy calls overlapping it on the shared
    session, and (2) timeouts interleaved with successes never trip the wedge-recovery reaper —
    each success resets the consecutive-timeout counter, so healthy traffic is never reaped out
    from under itself. Each round the healthy ``slow`` calls start *after* the victim so they
    provably overlap its 0.5s timeout window (instant calls would finish first and prove
    nothing). Across all rounds every healthy call must return its own correct answer.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    rounds, healthy_per_round = 8, 4
    try:
        await gw.call("find_symbol", {"name_path": "warm"})
        failures: list[str] = []
        for r in range(rounds):

            async def healthy(tag: str):
                await asyncio.sleep(0.2)  # in flight when the victim tears down at 0.5s
                try:
                    res = await gw.call("slow", {"seconds": 0.3, "marker": tag})
                    if (res.structuredContent or {}).get("echo") != tag:
                        failures.append(f"wrong echo for {tag}")
                except Exception as exc:  # noqa: BLE001 — record, don't abort the round
                    failures.append(f"{tag}: {exc!r}")

            async def timer():
                with pytest.raises(TimeoutError):
                    await gw.call("hang", {})

            tags = [f"r{r}-{i}" for i in range(healthy_per_round)]
            await asyncio.gather(timer(), *(healthy(t) for t in tags))

        assert not failures, f"{len(failures)} healthy calls failed under churn: {failures[:5]}"
    finally:
        await gw.aclose()


@pytest.mark.timeout(60)
async def test_caller_cancelled_during_cold_connect_does_not_storm(repo, monkeypatch):
    """A caller cancelled during the COLD connect must not respawn the child — no storm.

    The field failure: a serena call cancelled while the gateway was still performing the cold
    connect (spawn + MCP ``initialize``) let fastmcp force-close the half-open child, so the
    connect made no progress and the *next* call reconnected from scratch. Under parallel-
    subagent load that cancelled every connect attempt in turn — each spawning and immediately
    SIGKILLing a child (a ~30s respawn storm) while the originating call never completed (the
    35-minute hang). The warm-call cancellation tests above never caught it: the bug is unique
    to cancellation *before the session exists*.

    With the single-flight, shielded connect a cancelled caller only stops awaiting; the shared
    connect runs to completion for its siblings and its own retry. So however many callers are
    cancelled mid-connect, exactly ONE connect happens and a later call succeeds. On the pre-fix
    gateway the connect count climbs with each cancellation and this fails loudly.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "30")  # not exercising the call timeout
    # A slow cold boot opens a deterministic window to cancel *inside* the connect. The delay
    # must reach the child, so pass an explicit env (mcp's stdio default env would drop it).
    transport = StdioTransport(
        command=sys.executable,
        args=[str(FAKE)],
        cwd=str(repo),
        keep_alive=True,
        env={**os.environ, "FAKE_SERENA_BOOT_DELAY": "1.5"},
    )
    gw = gateway.SerenaGateway(str(repo), transport=transport)

    connects = 0
    original_open_locked = gw._open_locked

    async def counting_open_locked():
        nonlocal connects
        connects += 1
        return await original_open_locked()

    monkeypatch.setattr(gw, "_open_locked", counting_open_locked)

    try:
        # Repeatedly begin a cold call and cancel it well inside the 1.5s connect window.
        for _ in range(4):
            victim = asyncio.create_task(gw.call("find_symbol", {"name_path": "x"}))
            await asyncio.sleep(0.3)  # firmly mid cold-connect
            victim.cancel()
            with pytest.raises(asyncio.CancelledError):
                await victim

        # The shared connect survived every cancellation; a normal call now succeeds.
        result = await gw.call("find_symbol", {"name_path": "alive"})
        assert await _echo(result) == "alive", "shared connect did not survive caller cancellation"
        assert connects == 1, (
            f"cold connect respawned {connects - 1}x under caller cancellation — a cancelled connect "
            "is force-closing the shared child instead of running to completion (the respawn storm)"
        )
    finally:
        await gw.aclose()
