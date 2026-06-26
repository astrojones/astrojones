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
