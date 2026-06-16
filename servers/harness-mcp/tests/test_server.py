import json

from repo_agent_harness import server


def test_server_instructions_present():
    """The server ships concise, client-agnostic orientation cues."""
    text = server.mcp.instructions
    assert text
    assert "repo_context_overview" in text
    assert "repo_verify_changed" in text
    # Zero-footprint default: the navigation discipline and the explorer-preference
    # live in the always-read instructions, not in a per-repo AGENTS.md.
    assert "serena" in text
    assert "explorer" in text
    # Materialization is opt-in, surfaced here so the model knows the lever exists.
    assert "repo_bootstrap" in text


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


def test_read_range_blocks_code_when_not_onboarded(repo, monkeypatch):
    """Pre-onboarding, repo_read_range refuses code like native Read (the session-9e6fd520 escape)."""
    monkeypatch.chdir(repo)
    out = server.repo_read_range("src/payment.py")
    assert "content" not in out
    assert "serena_onboarding" in out["error"]


def test_read_range_allows_non_code_when_not_onboarded(repo, monkeypatch):
    monkeypatch.chdir(repo)
    out = server.repo_read_range("pyproject.toml")
    assert "content" in out


def test_read_range_allows_code_once_onboarded(repo, monkeypatch):
    monkeypatch.chdir(repo)
    mem = repo / ".serena" / "memories"
    mem.mkdir(parents=True)
    (mem / "core.md").write_text("onboarded\n")
    out = server.repo_read_range("src/payment.py")
    assert "def charge" in out["content"]


def test_read_range_env_escape_allows_code(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPO_AGENT_HARNESS_NO_SERENA_GATE", "1")
    out = server.repo_read_range("src/payment.py")
    assert "def charge" in out["content"]


def test_autoseed_onboards_fresh_repo(repo, monkeypatch):
    """Auto-seed writes a project_overview memory so a fresh repo is onboarded before the agent acts."""
    monkeypatch.chdir(repo)
    assert not server.serena_gate.is_onboarded(repo)
    server._autoseed_onboarding(str(repo))
    mem = repo / ".serena" / "memories" / "project_overview.md"
    assert mem.is_file()
    assert server.serena_gate.is_onboarded(repo)
    # the seeded memory is portable: no absolute paths committed
    assert str(repo) not in mem.read_text()


def test_autoseed_is_idempotent_and_preserves_existing(repo, monkeypatch):
    monkeypatch.chdir(repo)
    mem_dir = repo / ".serena" / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "core.md").write_text("real onboarding\n")
    server._autoseed_onboarding(str(repo))
    # already onboarded → does not overwrite or add the seed
    assert not (mem_dir / "project_overview.md").exists()
    assert (mem_dir / "core.md").read_text() == "real onboarding\n"


def test_autoseed_skips_when_gate_disabled(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPO_AGENT_HARNESS_NO_SERENA_GATE", "1")
    server._autoseed_onboarding(str(repo))
    assert not (repo / ".serena" / "memories" / "project_overview.md").exists()


def test_autoseed_noop_outside_repo():
    server._autoseed_onboarding(None)  # must not raise
