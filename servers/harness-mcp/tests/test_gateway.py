"""Tests for the Serena gateway (gateway.py) using a fake stdio Serena — no LSP, no network."""

import re
import sys
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
