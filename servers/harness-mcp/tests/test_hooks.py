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


def test_gate_blocks_code_read_when_not_onboarded(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    assert agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py")) is True


def test_gate_allows_non_code_read(tmp_path):
    (tmp_path / "README.md").write_text("# hi\n")
    assert agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "README.md")) is False


def test_gate_allows_code_read_when_onboarded(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    mem = tmp_path / ".serena" / "memories"
    mem.mkdir(parents=True)
    (mem / "core.md").write_text("onboarded\n")
    assert agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py")) is False


def test_gate_blocks_when_only_maintenance_memory(tmp_path):
    (tmp_path / "mod.py").write_text("x = 1\n")
    mem = tmp_path / ".serena" / "memories"
    mem.mkdir(parents=True)
    (mem / "memory_maintenance.md").write_text("conventions\n")
    assert agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py")) is True


def test_gate_env_escape_allows_code_read(tmp_path, monkeypatch):
    (tmp_path / "mod.py").write_text("x = 1\n")
    monkeypatch.setenv("REPO_AGENT_HARNESS_NO_SERENA_GATE", "1")
    assert agent_hooks._serena_gate_blocks(str(tmp_path), str(tmp_path / "mod.py")) is False


def test_gate_ignores_path_outside_repo(tmp_path):
    (tmp_path / "outside.py").write_text("x = 1\n")
    inner = tmp_path / "repo"
    inner.mkdir()
    assert agent_hooks._serena_gate_blocks(str(inner), str(tmp_path / "outside.py")) is False
