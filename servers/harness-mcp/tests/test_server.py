import json

import yaml
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
    # Onboarding is now automatic at connect: the old mandatory "FIRST action is
    # serena_initial_instructions" directive is gone (it wrongly implied a manual first step).
    assert "FIRST action" not in text


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
    """Auto-seed writes a cheap stub memory so a fresh repo is onboarded before the agent acts."""
    monkeypatch.chdir(repo)
    assert not server.serena_gate.is_onboarded(repo)
    server._autoseed_onboarding(str(repo))
    mem = repo / ".serena" / "memories" / "project_overview.md"
    assert mem.is_file()
    assert server.serena_gate.is_onboarded(repo)
    # the seed is a constant stub, not a rendered context overview
    assert mem.read_text() == server._ONBOARD_STUB


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


def test_repo_onboard_complete_marks_onboarded(repo, monkeypatch):
    """Confirming an ingest marks the repo onboarded in cognee state."""
    monkeypatch.chdir(repo)
    assert not server.paths.is_cognee_onboarded(str(repo))
    out = server.repo_onboard_complete("proj_dataset", ontology_key="default")
    assert out["ok"] is True
    assert out["onboarded"] is True
    assert out["dataset"] == "proj_dataset"
    assert server.paths.is_cognee_onboarded(str(repo))


def test_repo_onboard_complete_noop_outside_repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = server.repo_onboard_complete("proj_dataset")
    assert "error" in out


def test_seed_serena_languages_writes_all_present(repo, monkeypatch):
    """A repo with a secondary language gets every language seeded, not just the dominant one."""
    monkeypatch.chdir(repo)
    (repo / "web").mkdir()
    (repo / "web" / "app.ts").write_text("export const x = 1\n")
    server._seed_serena_languages(str(repo))
    data = yaml.safe_load((repo / ".serena" / "project.yml").read_text())
    # python dominant (3 files) ahead of the one .ts file, but both servers are activated
    assert data["languages"] == ["python", "typescript"]
    assert data["project_name"] == repo.name


def test_seed_serena_languages_merges_into_existing(repo, monkeypatch):
    """Serena writes project.yml with the dominant language only; the seed merges the rest in."""
    monkeypatch.chdir(repo)
    (repo / "web").mkdir()
    (repo / "web" / "app.ts").write_text("export const x = 1\n")
    cfg = repo / ".serena" / "project.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("project_name: keep\nlanguages:\n- python\nread_only: false\n")
    server._seed_serena_languages(str(repo))
    data = yaml.safe_load(cfg.read_text())
    assert data["languages"] == ["python", "typescript"]
    assert data["project_name"] == "keep"  # existing keys preserved across the merge
    assert data["read_only"] is False


def test_seed_serena_languages_idempotent(repo, monkeypatch):
    """When every present language is already listed, the file is left byte-for-byte untouched."""
    monkeypatch.chdir(repo)
    cfg = repo / ".serena" / "project.yml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text("languages:\n- python\n")
    before = cfg.read_text()
    server._seed_serena_languages(str(repo))
    assert cfg.read_text() == before


def test_seed_serena_languages_skips_when_gate_disabled(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("REPO_AGENT_HARNESS_NO_SERENA_GATE", "1")
    server._seed_serena_languages(str(repo))
    assert not (repo / ".serena" / "project.yml").exists()


def test_seed_serena_languages_noop_outside_repo():
    server._seed_serena_languages(None)  # must not raise
