"""Tests for the `repo-agent-harness hook <event>` CLI (Claude Code hook handlers)."""

import io
import json

from repo_agent_harness import cli


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
