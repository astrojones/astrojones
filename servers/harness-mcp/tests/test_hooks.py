"""Tests for the `repo-agent-harness hook <event>` CLI (Claude Code hook handlers)."""

import io
import json

from repo_agent_harness import agent_hooks, cli


def _run(payload, repo, monkeypatch, capsys, raw=None, event="pre-tool-use"):
    monkeypatch.chdir(repo)
    text = raw if raw is not None else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(text))
    rc = cli.main(["hook", event])
    return rc, json.loads(capsys.readouterr().out)


def test_pre_denies_rm_rf(repo, monkeypatch, capsys):
    rc, out = _run({"tool_name": "Bash", "tool_input": {"command": "rm -rf /"}}, repo, monkeypatch, capsys)
    assert rc == 0
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_allows_git_status(repo, monkeypatch, capsys):
    rc, out = _run({"tool_name": "Bash", "tool_input": {"command": "git status"}}, repo, monkeypatch, capsys)
    assert rc == 0
    assert out == {}


def test_pre_denies_secret_read(repo, monkeypatch, capsys):
    payload = {"tool_name": "Read", "tool_input": {"file_path": str(repo / ".env")}}
    rc, out = _run(payload, repo, monkeypatch, capsys)
    assert rc == 0
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_pre_fails_open_on_garbage(repo, monkeypatch, capsys):
    rc, out = _run(None, repo, monkeypatch, capsys, raw="not json at all")
    assert rc == 0
    assert out == {}


def test_hook_fails_open_outside_repo(tmp_path, monkeypatch, capsys):
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    rc, out = _run(payload, tmp_path, monkeypatch, capsys)
    assert rc == 0
    assert out == {}


def test_post_nudges_on_edit(repo, monkeypatch, capsys):
    rc, out = _run({"tool_name": "Edit", "tool_input": {}}, repo, monkeypatch, capsys, event="post-tool-use")
    assert rc == 0
    assert "verify" in out["hookSpecificOutput"]["additionalContext"].lower()


def test_post_quiet_on_read(repo, monkeypatch, capsys):
    rc, out = _run({"tool_name": "Read", "tool_input": {}}, repo, monkeypatch, capsys, event="post-tool-use")
    assert rc == 0
    assert out == {}


def _write_snapshot(repo, snap: dict) -> None:
    from repo_agent_harness import paths

    paths.perception_file(str(repo)).write_text(json.dumps(snap), encoding="utf-8")


def test_post_quiet_when_perception_green(repo, monkeypatch, capsys):
    _write_snapshot(repo, {"verdicts": [{"id": "lint", "kind": "lint", "ok": True, "summary": "passed"}], "git": {}})
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "payment.py")}}
    rc, out = _run(payload, repo, monkeypatch, capsys, event="post-tool-use")
    assert rc == 0
    assert out == {}  # green snapshot -> no nag (perception is handling verification)


def test_post_warns_when_perception_red(repo, monkeypatch, capsys):
    _write_snapshot(repo, {"verdicts": [{"id": "lint", "kind": "lint", "ok": False, "summary": "E501"}], "git": {}})
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "payment.py")}}
    rc, out = _run(payload, repo, monkeypatch, capsys, event="post-tool-use")
    assert rc == 0
    assert "lint" in out["hookSpecificOutput"]["additionalContext"]


def test_post_records_touched_and_nudges_without_snapshot(repo, monkeypatch, capsys):
    from repo_agent_harness import paths

    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "payment.py")}}
    rc, out = _run(payload, repo, monkeypatch, capsys, event="post-tool-use")
    assert rc == 0
    assert "verify" in out["hookSpecificOutput"]["additionalContext"].lower()  # no snapshot -> legacy nudge
    touched = json.loads(paths.perception_touched_file(str(repo)).read_text())
    assert "src/payment.py" in touched


def test_user_prompt_submit_reports_failure_then_silent(repo, monkeypatch, capsys):
    _write_snapshot(
        repo,
        {"verdicts": [{"id": "lint", "kind": "lint", "ok": False, "summary": "E501"}], "git": {"branch": "main"}},
    )
    rc1, out1 = _run({}, repo, monkeypatch, capsys, event="user-prompt-submit")
    assert rc1 == 0
    assert "FAILING" in out1["hookSpecificOutput"]["additionalContext"]
    # same snapshot, second turn: already surfaced -> no re-nag
    _rc2, out2 = _run({}, repo, monkeypatch, capsys, event="user-prompt-submit")
    assert out2 == {}


def test_user_prompt_submit_silent_when_green(repo, monkeypatch, capsys):
    _write_snapshot(repo, {"verdicts": [{"id": "lint", "kind": "lint", "ok": True}], "git": {"branch": "main"}})
    rc, out = _run({}, repo, monkeypatch, capsys, event="user-prompt-submit")
    assert rc == 0
    assert out == {}


def test_perception_deltas_branch_switch():
    cur = {"verdicts": [], "git": {"branch": "feature", "head": "b", "conflicted": []}}
    last = {"verdicts": [], "git": {"branch": "main", "head": "a", "conflicted": []}}
    assert any("branch switched" in line for line in agent_hooks._perception_deltas(cur, last))


def test_perception_deltas_recovery():
    cur = {"verdicts": [{"id": "lint", "kind": "lint", "ok": True}], "git": {}}
    last = {"verdicts": [{"id": "lint", "kind": "lint", "ok": False}], "git": {}}
    assert any("recovered" in line for line in agent_hooks._perception_deltas(cur, last))


def test_perception_deltas_new_conflict():
    cur = {"verdicts": [], "git": {"conflicted": ["a.py"]}}
    last = {"verdicts": [], "git": {"conflicted": []}}
    assert any("conflict" in line for line in agent_hooks._perception_deltas(cur, last))


def test_gate_blocks_code_read_when_not_onboarded(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    blocks, msg = agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py"))
    assert blocks is True and msg


def test_gate_allows_non_code_read(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n")
    blocks, _ = agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "README.md"))
    assert blocks is False


def test_gate_denies_code_read_even_when_onboarded(tmp_path):
    """Persistent Serena preference: code reads denied regardless of onboarding status."""
    (tmp_path / "mod.py").write_text("x = 1\n")
    mem = tmp_path / ".serena" / "memories"
    mem.mkdir(parents=True)
    (mem / "core.md").write_text("onboarded\n")
    blocks, msg = agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py"))
    assert blocks is True and "Navigate by symbol" in msg


def test_gate_blocks_when_only_maintenance_memory(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    mem = tmp_path / ".serena" / "memories"
    mem.mkdir(parents=True)
    (mem / "memory_maintenance.md").write_text("conventions\n")
    blocks, msg = agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py"))
    assert blocks is True and "call serena_initial_instructions" in msg


def test_gate_env_escape_allows_code_read(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("x = 1\n")
    monkeypatch.setenv("REPO_AGENT_HARNESS_NO_SERENA_GATE", "1")
    blocks, _ = agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py"))
    assert blocks is False


def test_gate_ignores_path_outside_repo(tmp_path):
    (tmp_path / "outside.py").write_text("x = 1\n")
    inner = tmp_path / "repo"
    inner.mkdir()
    blocks, _ = agent_hooks._serena_gate_blocks(str(inner), str(tmp_path / "outside.py"))
    assert blocks is False


# --------------------------------------------------------------- session-start recall


def _wired_client(fake):
    from repo_agent_harness.cognee_client import CogneeClient

    return CogneeClient(**fake.client_kwargs())


def test_session_start_injects_recall(repo, monkeypatch):
    from tests.fake_cognee import FakeCognee

    monkeypatch.chdir(repo)
    fake = FakeCognee(datasets=["agent_sessions"])
    out = agent_hooks.session_start({}, client=_wired_client(fake))
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    assert "Durable-memory recall" in ctx
    # Recall synthesizes over the graph (GRAPH_COMPLETION), not raw chunk retrieval.
    assert "canned:GRAPH_COMPLETION" in ctx


def _recall_search_payload(fake):
    """The recorded POST /api/v1/search payload (the recall query), or None."""
    for method, path, payload in fake.requests:
        if method == "POST" and path == "/api/v1/search":
            return payload
    return None


def test_session_start_recall_scoped_to_onboarded_dataset(repo, monkeypatch):
    """When onboarded, recall queries the project's own dataset, not the span-all scope."""
    from repo_agent_harness import paths
    from tests.fake_cognee import FakeCognee

    monkeypatch.chdir(repo)
    paths.cognee_onboarded_file(str(repo)).write_text(json.dumps({"dataset": "proj-x"}), encoding="utf-8")
    fake = FakeCognee(datasets=["proj-x"])
    agent_hooks.session_start({}, client=_wired_client(fake))
    assert _recall_search_payload(fake).get("datasets") == ["proj-x"]


def test_session_start_recall_spans_all_when_not_onboarded(repo, monkeypatch):
    """No marker -> no dataset scope (user's default span-all), preserving multi-repo projects."""
    from tests.fake_cognee import FakeCognee

    monkeypatch.chdir(repo)
    fake = FakeCognee(datasets=["agent_sessions"])
    agent_hooks.session_start({}, client=_wired_client(fake))
    assert "datasets" not in _recall_search_payload(fake)


def test_recall_lines_keeps_long_completion():
    """A synthesized GRAPH_COMPLETION answer must not be clipped at the old 300-char cap."""
    long = "x" * 900
    assert agent_hooks._recall_lines([{"text": long}]) == [long]


def test_session_start_silent_when_unconfigured(repo, monkeypatch):
    from repo_agent_harness.cognee_client import CogneeClient

    monkeypatch.chdir(repo)
    out = agent_hooks.session_start({}, client=CogneeClient(url=None, auth=None, key=None))
    # Unconfigured cognee contributes no recall section; the other fail-open sections may.
    assert "Durable-memory recall" not in _ctx(out)


def test_session_start_fails_open_when_cognee_down(repo, monkeypatch):
    from tests.fake_cognee import FakeCognee

    monkeypatch.chdir(repo)
    fake = FakeCognee(datasets=["agent_sessions"])
    fake.transport_failures = 99
    out = agent_hooks.session_start({}, client=_wired_client(fake))
    assert "Durable-memory recall" not in _ctx(out)


def test_session_start_silent_on_timeout(repo, monkeypatch):
    """A hanging cognee must NOT delay session start past the bound — silent empty response."""
    import asyncio
    import time

    import httpx
    from repo_agent_harness.cognee_client import CogneeAuth, CogneeClient
    from tests import fake_cognee

    async def _hang(request):
        await asyncio.sleep(30)
        return httpx.Response(200, json=[])

    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPO_AGENT_HARNESS_RECALL_TIMEOUT_S", "0.2")
    client = CogneeClient(
        url=fake_cognee.BASE_URL,
        auth=CogneeAuth(fake_cognee.EMAIL, fake_cognee.PASSWORD),
        transport=httpx.MockTransport(_hang),
    )
    start = time.monotonic()
    out = agent_hooks.session_start({}, client=client)
    assert "Durable-memory recall" not in _ctx(out)
    assert time.monotonic() - start < 2, "recall must be cut at the bound, not the client timeout"


def test_session_start_silent_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = agent_hooks.session_start({})
    assert out == {}


def test_session_start_wired_into_cli(repo, monkeypatch, capsys):
    """`repo-agent-harness hook session-start` resolves the handler (unconfigured env)."""
    rc, out = _run({}, repo, monkeypatch, capsys, event="session-start")
    assert rc == 0
    # Unconfigured cognee env yields no recall section (other fail-open sections may appear).
    assert "Durable-memory recall" not in _ctx(out)


def _ctx(out):
    return out.get("hookSpecificOutput", {}).get("additionalContext", "")


def test_session_start_injects_symbol_map(repo, monkeypatch):
    from repo_agent_harness.cognee_client import CogneeClient

    monkeypatch.chdir(repo)
    out = agent_hooks.session_start({}, client=CogneeClient(url=None, auth=None, key=None))
    ctx = _ctx(out)
    assert "Repo symbol map" in ctx
    assert "charge" in ctx


def test_session_start_symbol_map_before_recall(repo, monkeypatch):
    from tests.fake_cognee import FakeCognee

    monkeypatch.chdir(repo)
    fake = FakeCognee(datasets=["agent_sessions"])
    out = agent_hooks.session_start({}, client=_wired_client(fake))
    ctx = _ctx(out)
    assert ctx.index("Repo symbol map") < ctx.index("Durable-memory recall")


def test_session_start_nudge_when_not_onboarded(repo, monkeypatch):
    """The onboarding nudge fires even when cognee is unconfigured (independent section)."""
    from repo_agent_harness.cognee_client import CogneeClient

    monkeypatch.chdir(repo)
    out = agent_hooks.session_start({}, client=CogneeClient(url=None, auth=None, key=None))
    assert "/astrojones:onboard" in _ctx(out)


def test_session_start_no_nudge_when_onboarded(repo, monkeypatch):
    from repo_agent_harness import paths
    from repo_agent_harness.cognee_client import CogneeClient

    monkeypatch.chdir(repo)
    paths.cognee_onboarded_file(str(repo)).write_text("{}", encoding="utf-8")
    out = agent_hooks.session_start({}, client=CogneeClient(url=None, auth=None, key=None))
    assert "/astrojones:onboard" not in _ctx(out)


def test_session_start_empty_when_onboarded_no_sources_and_unconfigured(tmp_path, monkeypatch):
    """All three sections empty (onboarded, no sources, unconfigured cognee) -> {}."""
    import subprocess

    from repo_agent_harness import paths
    from repo_agent_harness.cognee_client import CogneeClient

    def _git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    _git("init", "-q")
    _git("config", "user.email", "t@t.t")
    _git("config", "user.name", "t")
    (tmp_path / "README.md").write_text("# no sources here\n")
    _git("add", "-A")
    _git("commit", "-qm", "init")
    monkeypatch.chdir(tmp_path)
    paths.cognee_onboarded_file(str(tmp_path)).write_text("{}", encoding="utf-8")
    out = agent_hooks.session_start({}, client=CogneeClient(url=None, auth=None, key=None))
    assert out == {}
