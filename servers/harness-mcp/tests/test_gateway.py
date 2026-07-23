"""Tests for the Serena gateway (gateway.py) using a fake stdio Serena — no LSP, no network."""

import asyncio
import re
import sys
import time
from contextlib import suppress
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastmcp.client.transports import StdioTransport
from repo_agent_harness import gateway, health, serena_gate

pytestmark = pytest.mark.anyio

FAKE = Path(__file__).parent / "fake_serena.py"


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _fake_gateway(repo: Path) -> gateway.SerenaGateway:
    transport = StdioTransport(command=sys.executable, args=[str(FAKE)], cwd=str(repo), keep_alive=True)
    return gateway.SerenaGateway(str(repo), transport=transport)


# --------------------------------------------------------------------------- snapshot


def test_snapshot_matches_pin_and_names_are_valid():
    snap = gateway.load_snapshot()
    assert snap["pin"] == gateway.SERENA_PIN, "snapshot drifted from the pin; rerun gateway-snapshot"
    assert snap["tools"], "snapshot is empty; run repo-agent-harness gateway-snapshot"
    for entry in snap["tools"]:
        prefixed = gateway.TOOL_PREFIX + entry["name"]
        assert re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", prefixed), prefixed


def test_proxied_tools_do_not_connect(repo):
    # a gateway whose command cannot exist: building tools must never launch it
    transport = StdioTransport(command="/nonexistent/serena", args=[], cwd=str(repo), keep_alive=True)
    gw = gateway.SerenaGateway(str(repo), transport=transport)
    tools = gateway.proxied_tools(gw)
    assert tools
    assert all(t.name.startswith(gateway.TOOL_PREFIX) for t in tools)
    assert {"serena_find_symbol", "serena_get_diagnostics_for_file", "serena_onboarding"} <= {t.name for t in tools}


# ---------------------------------------------------------------------- arg aliases


def test_alias_map_is_gated_by_tool_schema():
    # canonical present, alias absent -> alias is active
    active = gateway._alias_map_for({"properties": {"name_path_pattern": {}, "relative_path": {}}})
    assert active == {
        "name_path": "name_path_pattern",
        "path": "relative_path",
        "file_path": "relative_path",
        "file": "relative_path",
    }
    # canonical absent -> alias dropped (never inject an unknown field)
    assert gateway._alias_map_for({"properties": {"depth": {}}}) == {}
    # alias name is itself a real field -> never shadow the genuine parameter
    assert "path" not in gateway._alias_map_for({"properties": {"relative_path": {}, "path": {}}})
    # missing / empty schema tolerated
    assert gateway._alias_map_for(None) == {}


def test_alias_map_reverse_name_path_for_edit_tools():
    # find_symbol declares name_path_pattern -> forward alias only
    fwd = gateway._alias_map_for({"properties": {"name_path_pattern": {}}})
    assert fwd.get("name_path") == "name_path_pattern"
    assert "name_path_pattern" not in fwd  # reverse inactive here
    # replace_symbol_body/insert_*/find_referencing_symbols/rename_symbol declare name_path -> reverse alias
    rev = gateway._alias_map_for({"properties": {"name_path": {}, "relative_path": {}, "body": {}}})
    assert rev.get("name_path_pattern") == "name_path"
    assert "name_path" not in rev  # forward inactive here


def test_alias_map_replace_content_repl():
    m = gateway._alias_map_for({"properties": {"relative_path": {}, "needle": {}, "repl": {}, "mode": {}}})
    assert m.get("replacement") == "repl"
    assert m.get("new_string") == "repl"


def test_normalize_arguments_rewrites_and_dedupes():
    aliases = {"name_path": "name_path_pattern"}
    assert gateway._normalize_arguments({"name_path": "Foo"}, aliases) == {"name_path_pattern": "Foo"}
    # explicit canonical wins when both are supplied; the alias is never forwarded
    both = gateway._normalize_arguments({"name_path": "A", "name_path_pattern": "B"}, aliases)
    assert both == {"name_path_pattern": "B"}
    # no aliases -> identity, same object (zero-copy fast path)
    original = {"x": 1}
    assert gateway._normalize_arguments(original, {}) is original


async def test_proxied_tool_normalizes_alias_before_forwarding():
    """serena_find_symbol called with the natural ``name_path`` reaches Serena as ``name_path_pattern``."""
    forwarded: dict = {}

    class _Recorder:
        async def call(self, name: str, arguments: dict):
            forwarded["name"] = name
            forwarded["arguments"] = arguments
            return SimpleNamespace(content=[], structuredContent={}, isError=False)

    tools = {t.name: t for t in gateway.proxied_tools(_Recorder())}
    await tools["serena_find_symbol"].run({"name_path": "ConstraintGroup", "include_body": True})
    assert forwarded["name"] == "find_symbol"
    assert "name_path" not in forwarded["arguments"]
    assert forwarded["arguments"] == {"name_path_pattern": "ConstraintGroup", "include_body": True}


# ------------------------------------------------------------------------ forwarding


async def test_call_round_trip(repo):
    gw = _fake_gateway(repo)
    try:
        result = await gw.call("find_symbol", {"name_path": "charge"})
        assert result.isError is False
        assert (result.structuredContent or {}).get("echo") == "charge"
    finally:
        await gw.aclose()


async def test_is_error_passes_through(repo):
    gw = _fake_gateway(repo)
    try:
        result = await gw.call("boom", {})
        assert result.isError is True
        assert "kaboom" in str(result.content)
    finally:
        await gw.aclose()


async def test_crash_then_recover(repo):
    gw = _fake_gateway(repo)
    try:
        with pytest.raises(Exception, match=r".*"):  # noqa: PT011 - transport failure type is fastmcp-internal
            await gw.call("crash", {})
        result = await gw.call("find_symbol", {"name_path": "again"})
        assert (result.structuredContent or {}).get("echo") == "again"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_call_times_out(repo, monkeypatch):
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            await gw.call("hang", {})
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_recovers_after_timeout(repo, monkeypatch):
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    try:
        with pytest.raises(TimeoutError, match="timed out"):
            await gw.call("hang", {})
        result = await gw.call("find_symbol", {"name_path": "again"})
        assert (result.structuredContent or {}).get("echo") == "again"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_poison_fingerprint_fails_fast_after_repeated_wedges(repo, monkeypatch):
    """A call that wedges Serena on identical input is refused (not re-dispatched) after the limit.

    Defends against the wedge -> reap -> re-dispatch-same-input -> re-wedge loop: a fresh child
    respawned after a wedge would just re-wedge on the same deterministic input forever. After
    ``_POISON_WEDGE_LIMIT`` consecutive wedges of one fingerprint, the gateway fails fast without
    dispatching, so the loop cannot run indefinitely.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    try:
        # 'hang' wedges every time and carries no args, so every call shares one fingerprint.
        for _ in range(gateway._POISON_WEDGE_LIMIT):
            with pytest.raises(TimeoutError, match="timed out"):
                await gw.call("hang", {})
        # The next identical call must be refused WITHOUT dispatching: fast + a poison-specific
        # message, never the per-call "timed out" surface (which would mean it was dispatched).
        start = time.monotonic()
        with pytest.raises(TimeoutError, match="poison"):
            await gw.call("hang", {})
        elapsed = time.monotonic() - start
        assert elapsed < 0.2, f"poisoned call should fail fast without dispatching, took {elapsed:.3f}s"
        # A different fingerprint is unaffected and still forwards normally.
        result = await gw.call("find_symbol", {"name_path": "ok"})
        assert (result.structuredContent or {}).get("echo") == "ok"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_successful_call_clears_poison_count(repo, monkeypatch):
    """A successful call resets its fingerprint's wedge count, so a recovered input is not poisoned."""
    gw = _fake_gateway(repo)
    try:
        fp = gateway._fingerprint("find_symbol", {"name_path": "ok"})
        # Pre-seed one wedge short of the limit; a success must clear it, not tip into poison.
        gw._poison_wedges[fp] = gateway._POISON_WEDGE_LIMIT - 1
        result = await gw.call("find_symbol", {"name_path": "ok"})
        assert (result.structuredContent or {}).get("echo") == "ok"
        assert fp not in gw._poison_wedges
    finally:
        await gw.aclose()


# --------------------------------------------------------------------- tool timeout env


def test_tool_timeout_default_exceeds_serena_dispatch(monkeypatch):
    # The middleware backstop must never pre-empt Serena's own dispatch reap.
    for name in (
        gateway.TOOL_TIMEOUT_ENV,
        gateway.SERENA_DISPATCH_TIMEOUT_ENV,
        gateway.SERENA_CONNECT_TIMEOUT_ENV,
        gateway.SERENA_TIMEOUT_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    assert gateway.tool_timeout() == gateway._DEFAULT_TOOL_TIMEOUT
    assert gateway.tool_timeout() > gateway.serena_dispatch_timeout()


def test_tool_timeout_env_override(monkeypatch):
    monkeypatch.setenv(gateway.TOOL_TIMEOUT_ENV, "7.5")
    assert gateway.tool_timeout() == pytest.approx(7.5)


def test_tool_timeout_env_invalid_falls_back(monkeypatch):
    monkeypatch.setenv(gateway.TOOL_TIMEOUT_ENV, "nonsense")
    assert gateway.tool_timeout() == gateway._DEFAULT_TOOL_TIMEOUT


def test_tool_timeout_error_carries_fields():
    err = gateway.ToolTimeoutError(tool="repo_search_text", timeout_s=120.0)
    assert err.tool == "repo_search_text"
    assert err.timeout_s == pytest.approx(120.0)
    assert "repo_search_text" in str(err)
    assert "120" in str(err)


# ------------------------------------------------------------------- in-flight registry


def test_in_flight_snapshot_empty_by_default(repo):
    gw = gateway.SerenaGateway(str(repo))
    assert gw.in_flight_snapshot() == []


def test_register_inflight_shows_in_snapshot_then_clears(repo):
    gw = gateway.SerenaGateway(str(repo))
    with gw.register_inflight("repo_read_range", "/tmp/work"):
        snap = gw.in_flight_snapshot()
        assert len(snap) == 1
        entry = snap[0]
        assert entry["tool"] == "repo_read_range"
        assert entry["cwd"] == "/tmp/work"
        assert entry["elapsed_s"] >= 0.0
        assert entry["stalled"] is False
    assert gw.in_flight_snapshot() == []


def test_register_inflight_clears_on_exception(repo):
    gw = gateway.SerenaGateway(str(repo))
    err = RuntimeError("kaboom")
    with suppress(RuntimeError), gw.register_inflight("boom", "/tmp"):
        raise err
    assert gw.in_flight_snapshot() == []


def test_in_flight_stalled_flag(repo, monkeypatch):
    gw = gateway.SerenaGateway(str(repo))
    times = iter([100.0, 100.0 + gateway._INFLIGHT_STALL_SECONDS + 1.0])
    monkeypatch.setattr(gateway, "_inflight_clock", lambda: next(times))
    with gw.register_inflight("slow_tool", "/tmp"):
        snap = gw.in_flight_snapshot()
        assert snap[0]["stalled"] is True
        assert snap[0]["elapsed_s"] > gateway._INFLIGHT_STALL_SECONDS


def test_register_inflight_concurrent_entries(repo):
    gw = gateway.SerenaGateway(str(repo))
    with gw.register_inflight("a", "/x"), gw.register_inflight("b", "/y"):
        tools = {e["tool"] for e in gw.in_flight_snapshot()}
        assert tools == {"a", "b"}
    assert gw.in_flight_snapshot() == []


class _HangingClose:
    """Wraps a live client but makes ``close()`` block forever and reports disconnected.

    ``is_connected()`` returns False to force the next call to reconnect (and thus discard
    this client); ``close()`` then hangs — modelling a child whose teardown never returns.
    Used to prove a hung close does not wedge the gateway lock (FIX C).
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    def is_connected(self) -> bool:
        return False

    async def close(self) -> None:
        await anyio.sleep_forever()

    async def __aenter__(self):
        return self._inner

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.mark.timeout(30)
async def test_hung_close_does_not_wedge_lock(repo, monkeypatch):
    # FIX C root cause: a child whose close() never returns must not wedge ``self._lock``.
    # _discard_locked bounds the close with serena_close_timeout(); after the budget it is
    # abandoned, the lock is released, and the reconnect proceeds. Without the bound the
    # reconnect's discard awaits close() forever under the lock and the call never returns.
    monkeypatch.setenv(gateway.SERENA_CLOSE_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    try:
        first = await gw.call("find_symbol", {"name_path": "a"})
        assert (first.structuredContent or {}).get("echo") == "a"
        gw._client = _HangingClose(gw._client)
        result = await gw.call("find_symbol", {"name_path": "b"})
        assert (result.structuredContent or {}).get("echo") == "b"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_dispatch_bounded_when_lock_wedged(repo, monkeypatch):
    # FIX A backstop: serena_connect_timeout only wraps __aenter__, NOT the lock acquisition,
    # so a wedged lock leaves the connect/lock-wait outside any per-dispatch deadline. The
    # outer fail_after(serena_dispatch_timeout()) must bound the whole call() so a wedged lock
    # surfaces a TimeoutError instead of hanging forever at ``async with self._lock``.
    monkeypatch.setenv(gateway.SERENA_DISPATCH_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    holding = asyncio.Event()

    async def _hold_lock() -> None:
        # Wedge the lock from a *separate* task: anyio.Lock refuses same-task re-acquire with a
        # RuntimeError, so the call must contend from a different task to block at the lock.
        async with gw._lock:
            holding.set()
            await asyncio.sleep(3600)

    holder = asyncio.ensure_future(_hold_lock())
    try:
        await holding.wait()
        with pytest.raises(TimeoutError, match="dispatch"):
            await gw.call("find_symbol", {"name_path": "x"})
    finally:
        holder.cancel()
        with suppress(BaseException):
            await holder
        await gw.aclose()


class _HangingConnect:
    """A client whose connect (``__aenter__``) blocks forever — to test the connect timeout."""

    def is_connected(self) -> bool:
        return False

    async def __aenter__(self):
        await anyio.sleep_forever()

    async def __aexit__(self, *exc) -> bool:
        return False

    async def close(self) -> None:
        return None


@pytest.mark.timeout(30)
async def test_connect_times_out(repo, monkeypatch):
    # The connect (spawn + LSP boot) must be time-bounded too, not just the forwarded call —
    # a stuck startup previously hung unbounded and wedged the gateway lock.
    monkeypatch.setenv(gateway.SERENA_CONNECT_TIMEOUT_ENV, "0.5")
    gw = _fake_gateway(repo)
    monkeypatch.setattr(gw, "_ensure_client", _HangingConnect)
    try:
        with pytest.raises(TimeoutError, match="connect timed out"):
            await gw.call("find_symbol", {"name_path": "x"})
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_recovers_after_connect_timeout(repo, monkeypatch):
    # After a connect timeout the half-opened client is discarded, so the next call must
    # respawn a fresh session and succeed rather than staying wedged. The first connect gets a
    # tight 0.5s budget (fires on the hanging client); the respawn gets a generous one so the
    # real subprocess spawn + MCP initialize is not itself throttled.
    budgets = iter([0.5])
    monkeypatch.setattr(gateway, "serena_connect_timeout", lambda: next(budgets, 30.0))
    gw = _fake_gateway(repo)
    real_ensure = gw._ensure_client
    calls = {"n": 0}

    def flaky_ensure():
        calls["n"] += 1
        return _HangingConnect() if calls["n"] == 1 else real_ensure()

    monkeypatch.setattr(gw, "_ensure_client", flaky_ensure)
    try:
        with pytest.raises(TimeoutError, match="connect timed out"):
            await gw.call("find_symbol", {"name_path": "x"})
        result = await gw.call("find_symbol", {"name_path": "again"})
        assert (result.structuredContent or {}).get("echo") == "again"
    finally:
        await gw.aclose()


@pytest.mark.timeout(30)
async def test_session_is_reused_across_sequential_calls(repo):
    # The persistent-session fix: sequential calls must reuse one connection, not
    # tear it down and re-init per call (the old per-call ``async with`` regression).
    gw = _fake_gateway(repo)
    try:
        first = await gw.call("find_symbol", {"name_path": "a"})
        client_after_first = gw._client
        assert client_after_first is not None
        assert client_after_first.is_connected()
        second = await gw.call("find_symbol", {"name_path": "b"})
        assert gw._client is client_after_first, "session was rebuilt between sequential calls"
        assert (first.structuredContent or {}).get("echo") == "a"
        assert (second.structuredContent or {}).get("echo") == "b"
    finally:
        await gw.aclose()


async def test_aclose_disconnects_the_session(repo):
    gw = _fake_gateway(repo)
    await gw.call("find_symbol", {"name_path": "x"})
    assert gw._client is not None and gw._client.is_connected()
    await gw.aclose()
    assert gw._client is None


@pytest.mark.timeout(30)
async def test_concurrent_calls_share_session(repo):
    gw = _fake_gateway(repo)
    results: dict[int, str] = {}

    async def one(i: int) -> None:
        result = await gw.call("find_symbol", {"name_path": str(i)})
        results[i] = (result.structuredContent or {}).get("echo")

    try:
        async with anyio.create_task_group() as tg:
            for i in range(8):
                tg.start_soon(one, i)
        assert results == {i: str(i) for i in range(8)}
    finally:
        await gw.aclose()


async def test_proxied_tool_run_maps_result(repo):
    gw = _fake_gateway(repo)
    try:
        tool = next(t for t in gateway.proxied_tools(gw) if t.name == "serena_find_symbol")
        tool_result = await tool.run({"name_path": "x"})
        assert tool_result.is_error is False
        assert (tool_result.structured_content or {}).get("echo") == "x"
    finally:
        await gw.aclose()


async def test_warm_preconnects_in_background(repo):
    """warm() starts the shared session in the background so the first real call is hot."""
    gw = _fake_gateway(repo)
    try:
        task = gw.warm()
        assert task is not None
        await task
        assert gw._client is not None
        assert gw._client.is_connected()
        result = await gw.call("find_symbol", {"name_path": "x"})
        assert (result.structuredContent or {}).get("echo") == "x"
    finally:
        await gw.aclose()


# --------------------------------------------------------------------------- capability gate


def test_implementation_capability_predicate():
    # Python is refused (Pyright does not implement textDocument/implementation); message redirects.
    msg = serena_gate.implementation_unsupported("src/payment.py")
    assert msg and "find_referencing_symbols" in msg
    # Languages whose LSP implements the method pass (None == no refusal).
    assert serena_gate.implementation_unsupported("app/main.ts") is None
    assert serena_gate.implementation_unsupported("pkg/server.go") is None
    assert serena_gate.implementation_unsupported("src/lib.rs") is None
    # Empty / extensionless paths defer to Serena's own argument validation.
    assert serena_gate.implementation_unsupported("") is None
    assert serena_gate.implementation_unsupported("Makefile") is None
    # Only find_implementations is gated.
    assert serena_gate.capability_gate_for("find_implementations") is not None
    assert serena_gate.capability_gate_for("find_symbol") is None


async def test_find_implementations_refused_for_python_without_connecting():
    # A gateway whose command cannot exist: a working gate returns the refusal WITHOUT launching
    # the (nonexistent) child. Failing to gate would instead attempt to connect and raise.
    transport = StdioTransport(command="/nonexistent/serena", args=[], cwd=".", keep_alive=True)
    gw = gateway.SerenaGateway(".", transport=transport)
    tool = next(t for t in gateway.proxied_tools(gw) if t.name == "serena_find_implementations")
    result = await tool.run({"name_path": "X", "relative_path": "src/payment.py"})
    assert result.is_error is True
    assert "textDocument/implementation" in result.content[0].text


async def test_find_implementations_forwards_for_capable_language(repo):
    gw = _fake_gateway(repo)
    try:
        tool = next(t for t in gateway.proxied_tools(gw) if t.name == "serena_find_implementations")
        result = await tool.run({"name_path": "X", "relative_path": "app/main.ts"})
        assert result.is_error is False
        assert (result.structured_content or {}).get("echo") == "X"
    finally:
        await gw.aclose()


async def test_capability_gate_honors_disable_env(repo, monkeypatch):
    # With the gate disabled, even a Python target forwards to Serena (no refusal).
    monkeypatch.setenv(serena_gate.GATE_ENV, "1")
    gw = _fake_gateway(repo)
    try:
        tool = next(t for t in gateway.proxied_tools(gw) if t.name == "serena_find_implementations")
        result = await tool.run({"name_path": "X", "relative_path": "src/payment.py"})
        assert result.is_error is False
        assert (result.structured_content or {}).get("echo") == "X"
    finally:
        await gw.aclose()


# ------------------------------------------------------------------------- health glue


@pytest.mark.parametrize(
    ("cmdline", "expected"),
    [
        pytest.param([], False, id="empty"),
        pytest.param(["py", "serena", "start-mcp-server", "--project-from-cwd"], False, id="uvx-no-project"),
        pytest.param(
            ["py", "/v3.2.0/bin/serena", "start-mcp-server", "--project", "/repo"],
            True,
            id="other-version-same-repo",
        ),
        pytest.param(["py", "/cur/bin/serena", "start-mcp-server", "--project", "/repo"], False, id="our-version"),
        pytest.param(["py", "/v3.2.0/bin/serena", "start-mcp-server", "--project", "/other"], False, id="other-repo"),
        pytest.param(["py", "/v3.2.0/bin/serena", "start-mcp-server", "--project"], False, id="dangling-project"),
    ],
)
def test_is_stale_serena_child_selection(cmdline, expected):
    assert gateway._is_stale_serena_child(cmdline, "/cur/bin/serena", "/repo") is expected


def test_reap_kills_only_other_version_same_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(gateway, "serena_command", lambda: "/cur/bin/serena")
    root = str(tmp_path)
    root_resolved = str(Path(root).resolve())
    killed: list[int] = []

    class FakeProc:
        def __init__(self, pid: int, cmdline: list[str]) -> None:
            self.pid = pid
            self.info = {"pid": pid, "cmdline": cmdline}

        def terminate(self) -> None:
            killed.append(self.pid)

        def kill(self) -> None:  # pragma: no cover - wait_procs reports none alive
            killed.append(self.pid)

    procs = [
        FakeProc(111, ["py", "/v3.2.0/bin/serena", "start-mcp-server", "--project", root_resolved]),
        FakeProc(222, ["py", "/cur/bin/serena", "start-mcp-server", "--project", root_resolved]),
        FakeProc(333, ["py", "/v3.2.0/bin/serena", "start-mcp-server", "--project", "/elsewhere"]),
        FakeProc(444, ["uvx", "serena", "start-mcp-server", "--project-from-cwd"]),
    ]
    fake_psutil = SimpleNamespace(
        Error=Exception,
        process_iter=lambda attrs=None: procs,
        wait_procs=lambda victims, timeout=None: ([], []),
    )
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)

    reaped = gateway.reap_stale_serena_children(root)
    assert reaped == [111]
    assert killed == [111]


def test_reap_noop_for_bare_path_command(monkeypatch):
    monkeypatch.setattr(gateway, "serena_command", lambda: "serena")
    assert gateway.reap_stale_serena_children("/repo") == []


class _FakeChild:
    """Minimal psutil.Process child stub exposing .pid and .cmdline() for discovery tests."""

    def __init__(self, pid: int, cmdline: list[str]) -> None:
        self.pid = pid
        self._cmdline = cmdline

    def cmdline(self) -> list[str]:
        return self._cmdline


def _fake_psutil(children: list[_FakeChild]) -> SimpleNamespace:
    return SimpleNamespace(
        Error=Exception,
        Process=lambda: SimpleNamespace(children=lambda: children),
    )


def test_discover_child_pid_picks_serena_not_lower_sibling(monkeypatch, tmp_path):
    """The freshly spawned Serena child must be identified by its argv, not by lowest PID.

    A sibling subprocess (lint/typecheck/git from the perception daemon or a health check) can
    fork into the connect window and grab a *lower* PID than Serena. The naive ``min(new)`` diff
    adopts that sibling; the watchdog then kills a short-lived lint process while the real wedged
    Serena is never reaped (issue #30 follow-up). Discovery must select the ``start-mcp-server``
    child regardless of PID ordering.
    """
    root = str(tmp_path)
    before = {100}
    # Lower PID, but NOT serena (a perception-daemon lint subprocess); higher PID is the real serena.
    sibling = _FakeChild(101, ["/usr/bin/ruff", "check", "--quiet", root])
    serena = _FakeChild(202, ["py", "/cur/bin/serena", "start-mcp-server", "--project", root])
    children = [_FakeChild(100, ["py", "old-child"]), sibling, serena]
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil(children))

    assert gateway._discover_child_pid(before) == 202


def test_discover_child_pid_matches_project_root(monkeypatch, tmp_path):
    """Launch args carrying ``--project <root>`` pin discovery to this repo's child only."""
    root = str(tmp_path)
    before: set[int] = set()
    # Lower PID serena belongs to another project; higher PID serena is ours (matches --project root).
    other = _FakeChild(300, ["py", "serena", "start-mcp-server", "--project", "/elsewhere"])
    ours = _FakeChild(301, ["py", "serena", "start-mcp-server", "--project", root])
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([other, ours]))

    assert gateway._discover_child_pid(before, ["start-mcp-server", "--project", root]) == 301


def test_discover_child_pid_none_without_serena_child(monkeypatch):
    """No new child matches the launch markers (e.g. injected in-process transport) -> None."""
    sibling = _FakeChild(101, ["/usr/bin/ruff", "check"])
    monkeypatch.setitem(sys.modules, "psutil", _fake_psutil([sibling]))

    assert gateway._discover_child_pid({100}) is None


def test_diagnostics_check_counts_from_fake_result(repo):
    (repo / "src" / "payment.py").write_text("def charge():\n    return 2\n")
    canned = SimpleNamespace(
        structuredContent={
            "src/payment.py": {
                "ERROR": {"<file>": [{"message": "e1"}]},
                "WARNING": {"<file>": [{"message": "w1"}, {"message": "w2"}]},
            }
        },
        content=[],
    )

    class FakeGateway:
        def call_from_thread(self, name: str, arguments: dict) -> SimpleNamespace:
            assert name == "get_diagnostics_for_file"
            return canned

    snap = health.run(str(repo), only="diagnostics", gateway=FakeGateway())
    (check,) = snap.checks
    assert check.ok is False
    assert "1 error(s), 2 warning(s)" in check.summary


async def test_diagnostics_live_through_fake_serena(repo):
    (repo / "src" / "payment.py").write_text("def charge():\n    return 2\n")
    gw = _fake_gateway(repo)
    try:
        snap = await anyio.to_thread.run_sync(
            lambda: health.run(str(repo), only="diagnostics", refresh=True, gateway=gw)
        )
        (check,) = snap.checks
        assert check.skipped is False
        assert check.ok is False, check.summary
        assert "error" in check.summary
    finally:
        await gw.aclose()


# ---------------------------------------------------------------------------- server


def test_server_exposes_proxied_serena_tools():
    import asyncio

    from repo_agent_harness import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"serena_find_symbol", "serena_initial_instructions", "repo_health"} <= names


# ----------------------------------------------------------------- launch / command


def test_serena_args_launch_the_installed_server_not_uvx():
    args = gateway.serena_args("/repo")
    assert args[0] == "start-mcp-server"
    assert "--from" not in args
    assert not any("uvx" in a or "git+https" in a for a in args)
    assert args[args.index("--project") + 1] == "/repo"


def test_serena_command_env_override_wins(monkeypatch):
    monkeypatch.setenv(gateway.SERENA_CMD_ENV, "/custom/serena")
    assert gateway.serena_command() == "/custom/serena"


def test_serena_command_prefers_script_next_to_interpreter(monkeypatch, tmp_path):
    monkeypatch.delenv(gateway.SERENA_CMD_ENV, raising=False)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "serena").write_text("")
    monkeypatch.setattr(gateway.sys, "executable", str(bindir / "python"))
    assert gateway.serena_command() == str(bindir / "serena")


def test_serena_command_falls_back_to_path_name(monkeypatch, tmp_path):
    monkeypatch.delenv(gateway.SERENA_CMD_ENV, raising=False)
    bindir = tmp_path / "empty"
    bindir.mkdir()
    monkeypatch.setattr(gateway.sys, "executable", str(bindir / "python"))
    assert gateway.serena_command() == "serena"


def test_ensure_client_wires_serena_command_into_the_transport(monkeypatch, tmp_path):
    # No injected transport: _ensure_client must build the child launch from
    # serena_command() + serena_args(), never the old uvx path — and never connect.
    captured = {}

    def fake_transport(*, command, args, cwd, keep_alive):
        captured.update(command=command, args=args, cwd=cwd, keep_alive=keep_alive)
        return "TRANSPORT"

    monkeypatch.setattr(gateway, "StdioTransport", fake_transport)
    monkeypatch.setattr(gateway, "Client", lambda transport: ("CLIENT", transport))
    monkeypatch.setenv(gateway.SERENA_CMD_ENV, "/opt/serena")

    gw = gateway.SerenaGateway(str(tmp_path))
    client = gw._ensure_client()

    assert client == ("CLIENT", "TRANSPORT")
    assert captured["command"] == "/opt/serena"
    assert captured["args"] == gateway.serena_args(str(tmp_path))
    assert captured["cwd"] == str(tmp_path)
    assert captured["keep_alive"] is True


def test_pyproject_pins_serena_to_the_same_sha():
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    m = re.search(r"oraios/serena@([0-9a-f]{40})", text)
    assert m, "serena-agent git dependency missing from pyproject.toml"
    assert m.group(1) == gateway.SERENA_PIN, "pyproject serena pin drifted from gateway.SERENA_PIN"
    assert "serena-agent @ git+https://github.com/oraios/serena@" in text


def test_connect_lock_acquire_and_release(tmp_path, monkeypatch):
    """The connect flock is taken for a real root and releasing frees it for the next holder."""
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path))
    fd = gateway._acquire_connect_lock(str(tmp_path), timeout=1.0)
    assert fd is not None
    lock_file = tmp_path / "repos"
    assert any(lock_file.rglob("connect.lock"))
    gateway._release_connect_lock(fd)
    fd2 = gateway._acquire_connect_lock(str(tmp_path), timeout=1.0)
    assert fd2 is not None
    gateway._release_connect_lock(fd2)


def test_connect_lock_fails_open_when_held(tmp_path, monkeypatch):
    """A held lock makes a second acquirer wait, then fail open (None) — never block forever."""
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path))
    holder = gateway._acquire_connect_lock(str(tmp_path), timeout=1.0)
    assert holder is not None
    start = time.monotonic()
    fd = gateway._acquire_connect_lock(str(tmp_path), timeout=0.5)
    waited = time.monotonic() - start
    assert fd is None
    assert waited >= 0.4, "second acquirer must actually wait for the timeout before failing open"
    gateway._release_connect_lock(holder)


def test_connect_lock_none_root_fails_open():
    """No resolved root (injected transports/tests) → no lock, connect proceeds."""
    assert gateway._acquire_connect_lock(None, timeout=1.0) is None
    gateway._release_connect_lock(None)  # no-op, must not raise


def test_proxied_tools_retire_serena_memory_surface(repo):
    """The six serena memory tools are filtered out — cognee (mem_*) is the only memory surface."""
    transport = StdioTransport(command="/nonexistent/serena", args=[], cwd=str(repo), keep_alive=True)
    gw = gateway.SerenaGateway(str(repo), transport=transport)
    names = {t.name for t in gateway.proxied_tools(gw)}
    retired = {gateway.TOOL_PREFIX + bare for bare in gateway._RETIRED_MEMORY_TOOLS}
    assert not (names & retired), f"retired memory tools leaked through the proxy: {names & retired}"
    # The snapshot itself still carries them (it mirrors upstream verbatim).
    snapshot_names = {t["name"] for t in gateway.load_snapshot()["tools"]}
    assert gateway._RETIRED_MEMORY_TOOLS <= snapshot_names
    # Code-intelligence tools are untouched.
    assert {"serena_find_symbol", "serena_onboarding", "serena_rename_symbol"} <= names
