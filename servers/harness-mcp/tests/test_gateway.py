"""Tests for the Serena gateway (gateway.py) using a fake stdio Serena — no LSP, no network."""

import re
import sys
from pathlib import Path
from types import SimpleNamespace

import anyio
import pytest
from fastmcp.client.transports import StdioTransport
from harness import gateway, health

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


async def test_proxied_tool_run_maps_result(repo):
    gw = _fake_gateway(repo)
    try:
        tool = next(t for t in gateway.proxied_tools(gw) if t.name == "serena_find_symbol")
        tool_result = await tool.run({"name_path": "x"})
        assert tool_result.is_error is False
        assert (tool_result.structured_content or {}).get("echo") == "x"
    finally:
        await gw.aclose()


# ------------------------------------------------------------------------- health glue


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

    from harness import server

    names = {t.name for t in asyncio.run(server.mcp.list_tools())}
    assert {"serena_find_symbol", "serena_initial_instructions", "repo_health"} <= names
