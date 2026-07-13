"""Serena HTTP daemon mode: discovery, identity, spawn, sharing, timeouts, lifecycle.

The counterpart of the stdio suite for the one-daemon-per-worktree architecture. A fake
Serena serves streamable-http (see fake_serena.py); tests cover the daemon manager
(``serena_daemon``) and the gateway's HTTP path end to end — including the test that gates
the absence of the stdio watchdog machinery here: a hung call must produce a PROMPT
cooperative TimeoutError over HTTP.
"""

import asyncio
import os
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest
from repo_agent_harness import gateway, serena_daemon

pytestmark = [pytest.mark.anyio, pytest.mark.timeout(120)]

_FAKE = str(Path(__file__).parent / "fake_serena.py")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def http_env(monkeypatch):
    """HTTP transport on, and no real serena spawnable by accident."""
    monkeypatch.setenv(serena_daemon.SERENA_TRANSPORT_ENV, "http")
    monkeypatch.setenv(gateway.SERENA_CMD_ENV, "/nonexistent/serena")


@pytest.fixture
def fake_wrapper(tmp_path_factory, monkeypatch):
    """A SERENA_CMD executable that launches fake_serena with Serena's real daemon argv."""
    wrapper = tmp_path_factory.mktemp("bin") / "fake-serena"
    wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{_FAKE}" "$@"\n')
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(gateway.SERENA_CMD_ENV, str(wrapper))
    return wrapper


def _start_fake_daemon(root: str, port: int, project_name: str | None = None) -> subprocess.Popen:
    env = dict(os.environ)
    if project_name is not None:
        env["FAKE_SERENA_PROJECT"] = project_name
    return subprocess.Popen(  # noqa: S603
        [
            sys.executable,
            _FAKE,
            "start-mcp-server",
            "--project",
            root,
            "--transport",
            "streamable-http",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def _wait_ours(port: int, marker: str, budget: float = 30.0) -> None:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if await serena_daemon._probe(port, marker) == "ours":
            return
        await asyncio.sleep(0.2)
    msg = f"fake daemon on {port} never became ready"
    raise TimeoutError(msg)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# ------------------------------------------------------------------ pre-running daemon


async def test_http_gateway_uses_prerunning_daemon_and_leaves_it_alive(repo, http_env):
    """Connect to an existing daemon; aclose closes the client only — the daemon persists."""
    root = str(repo)
    port = serena_daemon.base_port(root)
    proc = _start_fake_daemon(root, port)
    try:
        await _wait_ours(port, serena_daemon.identity_marker(root))
        gw = gateway.SerenaGateway(root)
        result = await gw.call("find_symbol", {"name_path": "X"})
        assert (result.structuredContent or {}).get("echo") == "X"
        assert gw._child_pid is None  # the daemon is NOT our child
        await gw.aclose()
        assert proc.poll() is None, "aclose must not stop the shared daemon"
        # A second gateway (another 'process') reuses the same daemon.
        gw2 = gateway.SerenaGateway(root)
        result2 = await gw2.call("find_symbol", {"name_path": "Y"})
        assert (result2.structuredContent or {}).get("echo") == "Y"
        await gw2.aclose()
    finally:
        proc.kill()
        proc.wait()


async def test_http_parallel_gateways_share_one_daemon(repo, http_env):
    """Two gateways calling concurrently are served by ONE daemon process."""
    root = str(repo)
    port = serena_daemon.base_port(root)
    proc = _start_fake_daemon(root, port)
    try:
        await _wait_ours(port, serena_daemon.identity_marker(root))
        gws = [gateway.SerenaGateway(root) for _ in range(2)]
        results = await asyncio.gather(
            *(gw.call("slow", {"seconds": 0.2, "marker": f"m{i}"}) for i, gw in enumerate(gws))
        )
        assert [(r.structuredContent or {}).get("echo") for r in results] == ["m0", "m1"]
        for gw in gws:
            await gw.aclose()
        assert proc.poll() is None
    finally:
        proc.kill()
        proc.wait()


# ----------------------------------------------------------------------- spawn path


async def test_http_gateway_spawns_daemon_and_stop_cli_kills_it(repo, http_env, fake_wrapper):
    """No daemon running: the gateway spawns one detached, records state; stop_daemon ends it."""
    root = str(repo)
    gw = gateway.SerenaGateway(root)
    try:
        result = await gw.call("find_symbol", {"name_path": "Z"})
        assert (result.structuredContent or {}).get("echo") == "Z"
        state = serena_daemon.read_state(root)
        assert state is not None
        assert state["port"] == serena_daemon.base_port(root)
        assert _alive(state["pid"])
    finally:
        await gw.aclose()
    out = serena_daemon.stop_daemon(root)
    assert out["stopped"] is True
    deadline = time.monotonic() + 5
    while _alive(out["pid"]) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(out["pid"])
    assert serena_daemon.read_state(root) is None


async def test_http_identity_mismatch_probes_past_squatter(repo, http_env, fake_wrapper):
    """A foreign daemon on the base port is skipped; ours spawns on the next port."""
    root = str(repo)
    base = serena_daemon.base_port(root)
    squatter = _start_fake_daemon(root, base, project_name="someone-elses-project")
    try:
        await _wait_ours(base, "at someone-elses-project is activated")
        gw = gateway.SerenaGateway(root)
        try:
            result = await gw.call("find_symbol", {"name_path": "Q"})
            assert (result.structuredContent or {}).get("echo") == "Q"
            state = serena_daemon.read_state(root)
            assert state is not None
            assert state["port"] == base + 1
        finally:
            await gw.aclose()
            serena_daemon.stop_daemon(root)
    finally:
        squatter.kill()
        squatter.wait()


async def test_http_daemon_kill9_recovers_by_respawn(repo, http_env, fake_wrapper):
    """Kill -9 on the daemon mid-session: the failed call reaps, the next call respawns."""
    root = str(repo)
    gw = gateway.SerenaGateway(root)
    try:
        assert (await gw.call("find_symbol", {"name_path": "A"})).structuredContent["echo"] == "A"
        first_pid = serena_daemon.read_state(root)["pid"]
        os.kill(first_pid, signal.SIGKILL)
        with pytest.raises(BaseException):  # noqa: B017,PT011 - transport failure shape varies; reap is what matters
            await gw.call("find_symbol", {"name_path": "B"})
        result = await gw.call("find_symbol", {"name_path": "C"})
        assert (result.structuredContent or {}).get("echo") == "C"
        second_pid = serena_daemon.read_state(root)["pid"]
        assert second_pid != first_pid
    finally:
        await gw.aclose()
        serena_daemon.stop_daemon(root)


# ------------------------------------------------------------------ timeout behaviour


async def test_http_hung_call_times_out_promptly(repo, http_env, monkeypatch):
    """Cooperative fail_after genuinely bounds an HTTP call — the watchdog-deletion gate.

    Over stdio a hung read ignored cancellation (hence the out-of-band watchdog); httpx reads
    are cancellation-native, so a never-answering daemon must surface as a TimeoutError within
    the per-call budget, not hang.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "2")
    root = str(repo)
    port = serena_daemon.base_port(root)
    proc = _start_fake_daemon(root, port)
    try:
        await _wait_ours(port, serena_daemon.identity_marker(root))
        gw = gateway.SerenaGateway(root)
        started = time.monotonic()
        with pytest.raises(TimeoutError, match=r"timed out|hard-killed|exceeded"):
            await gw.call("hang", {})
        assert time.monotonic() - started < 30, "HTTP timeout must be prompt (cooperative)"
        await gw.aclose()
    finally:
        proc.kill()
        proc.wait()


# -------------------------------------------------------------------- daemon manager


async def test_probe_classifies_free_port(repo):
    port = serena_daemon.base_port(str(repo))
    assert await serena_daemon._probe(port, serena_daemon.identity_marker(str(repo))) == "free"


def test_state_roundtrip_and_stale_cleanup(repo):
    root = str(repo)
    serena_daemon._write_state(root, port=12345, pid=99999999, argv=["x"])
    assert serena_daemon.read_state(root)["port"] == 12345
    assert serena_daemon.clean_stale_state(root) is True  # pid is dead
    assert serena_daemon.read_state(root) is None
    # A live pid (our own) is kept.
    serena_daemon._write_state(root, port=1, pid=os.getpid(), argv=["x"])
    assert serena_daemon.clean_stale_state(root) is False
    assert serena_daemon.read_state(root) is not None


def test_identity_marker_is_path_based(repo):
    marker = serena_daemon.identity_marker(str(repo))
    assert str(repo.resolve()) in marker
    assert marker.endswith("is activated")


def test_stop_daemon_without_state_is_safe(repo):
    out = serena_daemon.stop_daemon(str(repo))
    assert out["ok"] is True
    assert out["stopped"] is False


def test_transport_env_switch(monkeypatch):
    monkeypatch.delenv(serena_daemon.SERENA_TRANSPORT_ENV, raising=False)
    assert serena_daemon.serena_transport() == "http"
    monkeypatch.setenv(serena_daemon.SERENA_TRANSPORT_ENV, "stdio")
    assert serena_daemon.serena_transport() == "stdio"
    monkeypatch.setenv(serena_daemon.SERENA_TRANSPORT_ENV, "nonsense")
    assert serena_daemon.serena_transport() == "http"
