import json

from repo_agent_harness import server


def test_tool_functions_callable(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert server.repo_context_overview()["root"] == str(repo)
    assert server.repo_policy_check_command("rm -rf /")["allowed"] is False
    assert server.repo_context_status()["branch"]


def test_tools_registered():
    import asyncio

    tools = asyncio.run(server.mcp.list_tools())
    names = {t.name for t in tools}
    expected = {
        "repo_context_overview",
        "repo_context_status",
        "repo_context_relevant_files",
        "repo_search_text",
        "repo_search_files",
        "repo_read_range",
        "repo_impact_file",
        "repo_verify_changed",
        "repo_diff_current",
        "repo_health",
        "repo_policy_check_command",
    }
    assert expected <= names


def test_res_impact_resource(repo, monkeypatch):
    monkeypatch.chdir(repo)
    result = json.loads(server.res_impact("src/payment.py"))
    assert "risk" in result
    assert result["risk"] == "high"
