"""Tests for Google Antigravity target: hooks and scaffold."""

import json

from repo_agent_harness import agent_hooks, scaffold


def test_antigravity_pre_denies_rm_rf(repo):
    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "rm -rf /"},
        },
        "workspacePaths": [str(repo)],
    }
    out = agent_hooks.antigravity_pre_tool_use(payload, root=str(repo))
    assert out.get("decision") == "deny"
    assert "blocked" in out.get("reason", "").lower() or "denied" in out.get("reason", "").lower()


def test_antigravity_pre_allows_safe_command(repo):
    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "git status"},
        },
        "workspacePaths": [str(repo)],
    }
    out = agent_hooks.antigravity_pre_tool_use(payload, root=str(repo))
    assert out.get("decision") == "allow"


def test_antigravity_pre_asks_for_confirmation(repo):
    payload = {
        "toolCall": {
            "name": "run_command",
            "args": {"CommandLine": "git push origin main"},
        },
        "workspacePaths": [str(repo)],
    }
    out = agent_hooks.antigravity_pre_tool_use(payload, root=str(repo))
    assert out.get("decision") == "ask"


def test_antigravity_pre_denies_secret_path(repo):
    secret_file = repo / ".env"
    secret_file.write_text("SECRET=1\n")
    payload = {
        "toolCall": {
            "name": "view_file",
            "args": {"AbsolutePath": str(secret_file)},
        },
        "workspacePaths": [str(repo)],
    }
    out = agent_hooks.antigravity_pre_tool_use(payload, root=str(repo))
    assert out.get("decision") == "deny"
    assert "secret path" in out.get("reason", "").lower()


def test_antigravity_pre_allows_normal_file(repo):
    normal_file = repo / "main.py"
    normal_file.write_text("print(1)\n")
    payload = {
        "toolCall": {
            "name": "view_file",
            "args": {"AbsolutePath": str(normal_file)},
        },
        "workspacePaths": [str(repo)],
    }
    out = agent_hooks.antigravity_pre_tool_use(payload, root=str(repo))
    assert out.get("decision") == "allow"


def test_antigravity_post_records_touched(repo):
    touched_file = repo / "edited.py"
    payload = {
        "toolCall": {
            "name": "write_to_file",
            "args": {"TargetFile": str(touched_file)},
        },
        "workspacePaths": [str(repo)],
    }
    out = agent_hooks.antigravity_post_tool_use(payload, root=str(repo))
    assert out == {}


def test_bootstrap_antigravity_target_creates_mcp_config(tmp_path):
    res = scaffold.bootstrap_repo(str(tmp_path), target="antigravity")
    assert res["ok"] is True
    assert ".agents/mcp_config.json" in res["created"]
    cfg_file = tmp_path / ".agents" / "mcp_config.json"
    assert cfg_file.is_file()
    cfg = json.loads(cfg_file.read_text())
    assert "repo-agent-harness" in cfg["mcpServers"]


def test_bootstrap_antigravity_idempotent(tmp_path):
    scaffold.bootstrap_repo(str(tmp_path), target="antigravity")
    res2 = scaffold.bootstrap_repo(str(tmp_path), target="antigravity")
    assert res2["ok"] is True
    assert ".agents/mcp_config.json" in res2["skipped"]


def test_bootstrap_all_target_creates_all(tmp_path):
    res = scaffold.bootstrap_repo(str(tmp_path), target="all", pin="abc1234")
    assert res["ok"] is True
    assert ".opencode/opencode.json" in res["created"]
    assert ".agents/mcp_config.json" in res["created"]
    assert ".mcp.json" in res["created"]
    ins = scaffold.inspect_bootstrap(str(tmp_path))
    assert ins["present"]["antigravity_mcp_config"] is True
    assert ins["present"]["opencode_json"] is True
    assert ins["present"]["mcp_json"] is True
