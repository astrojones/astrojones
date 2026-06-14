"""Tests for the per-repo bootstrap CLI subcommand.

The bootstrap is what the plugin's first-touch hook runs to materialize the
per-repo harness (agent/, AGENTS.md, optional .mcp.json, optional opencode
section). The CLI subcommand is the source of truth — the MCP ``repo_*`` tools
expose a read-only view of the same data.

The behavior is split off from ``init`` so ``init`` keeps its narrow "write
``.mcp.json`` for non-plugin environments" contract and ``bootstrap`` owns the
full first-touch surface. Both stay idempotent and section-merge based.
"""

from __future__ import annotations

import json

from harness import scaffold

# ---------------------------------------------------------------------------
# Default behavior (target="claude") — same shape as init, no surprise
# ---------------------------------------------------------------------------


def test_bootstrap_default_creates_agent_tree_and_skips_mcp_json(repo):
    """Default bootstrap writes agent/ but skips .mcp.json (which needs --pin)."""
    res = scaffold.bootstrap_repo(str(repo))
    assert res["ok"]
    assert (repo / "agent").is_dir()
    assert not (repo / ".mcp.json").is_file(), ".mcp.json only with --pin/--spec"
    assert not (repo / "AGENTS.md").is_file(), "default target skips AGENTS.md (opt-in via agents_md)"


def test_bootstrap_with_pin_writes_mcp_json(repo):
    """Bootstrap --pin writes .mcp.json with the pinned harness spec."""
    res = scaffold.bootstrap_repo(str(repo), pin="abc1234")
    assert res["ok"]
    assert (repo / ".mcp.json").is_file()
    cfg = json.loads((repo / ".mcp.json").read_text())
    assert "repo-agent-harness" in cfg["mcpServers"]
    assert any("@abc1234#subdirectory=mcp" in str(a) for a in cfg["mcpServers"]["repo-agent-harness"]["args"])


def test_bootstrap_opencode_target_creates_opencode_dir(repo):
    """target='opencode' creates .opencode/opencode.json with the harness wiring.

    Does NOT create .mcp.json (opencode reads opencode.json, not .mcp.json).
    """
    res = scaffold.bootstrap_repo(str(repo), target="opencode")
    assert res["ok"]
    assert not (repo / ".mcp.json").is_file()
    oc_path = repo / ".opencode" / "opencode.json"
    assert oc_path.is_file()
    cfg = json.loads(oc_path.read_text())
    # opencode.json has `mcp` (not `mcpServers`); the bootstrap must use the right key.
    assert "mcp" in cfg
    assert "repo-agent-harness" in cfg["mcp"]
    assert "command" in cfg["mcp"]["repo-agent-harness"]
    assert "skills" in cfg, "opencode surface should pre-declare the skills.paths entry"
    assert any("opencode-plugin" in p for p in cfg["skills"].get("paths", []))


def test_bootstrap_both_target_creates_claude_and_opencode_artifacts(repo):
    """target='both' writes agent/ (Claude side) and .opencode/opencode.json (opencode side)."""
    res = scaffold.bootstrap_repo(str(repo), target="both")
    assert res["ok"]
    assert (repo / "agent").is_dir()
    assert (repo / ".opencode" / "opencode.json").is_file()
    assert not (repo / ".mcp.json").is_file(), ".mcp.json only with --pin/--spec"


def test_bootstrap_opencode_section_is_idempotent(repo):
    """Two opencode-target bootstraps produce the same .opencode/opencode.json."""
    scaffold.bootstrap_repo(str(repo), target="opencode")
    first = (repo / ".opencode" / "opencode.json").read_text()
    scaffold.bootstrap_repo(str(repo), target="opencode")
    second = (repo / ".opencode" / "opencode.json").read_text()
    assert first == second


def test_bootstrap_opencode_merges_existing_opencode_json(repo):
    """If .opencode/opencode.json exists, bootstrap merges instead of replacing."""
    (repo / ".opencode").mkdir()
    (repo / ".opencode" / "opencode.json").write_text(
        json.dumps({"agent": {"my-agent": {"description": "custom", "mode": "subagent"}}}, indent=2)
    )
    res = scaffold.bootstrap_repo(str(repo), target="opencode")
    cfg = json.loads((repo / ".opencode" / "opencode.json").read_text())
    assert cfg["agent"]["my-agent"]["description"] == "custom", "user's agent must be preserved"
    assert "mcp" in cfg, "harness wiring must have been merged in"
    assert "repo-agent-harness" in cfg["mcp"]
    assert any("opencode.json" in s for s in res.get("merged", []) + res.get("skipped", []))


# ---------------------------------------------------------------------------
# Unknown target -> structured error
# ---------------------------------------------------------------------------


def test_bootstrap_unknown_target_returns_error(repo):
    """An unknown --target value fails loud, not silently."""
    res = scaffold.bootstrap_repo(str(repo), target="wat")
    assert res["ok"] is False
    assert "target" in res["error"]
    assert "wat" in res["error"]


# ---------------------------------------------------------------------------
# CLI subcommand
# ---------------------------------------------------------------------------


def test_cli_bootstrap_subcommand(repo, monkeypatch, capsys):
    """`bootstrap --target both --pin <sha> --agents-md overwrite` writes everything."""
    from harness import cli

    monkeypatch.chdir(repo)
    code = cli.main(
        [
            "bootstrap",
            "--target",
            "both",
            "--agents-md",
            "overwrite",
            "--pin",
            "deadbee",
        ]
    )
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"]
    assert (repo / ".mcp.json").is_file()
    assert (repo / "agent").is_dir()
    assert (repo / "AGENTS.md").is_file()
    assert (repo / ".opencode" / "opencode.json").is_file()


# ---------------------------------------------------------------------------
# MCP tool wrapper (read-only inspection of the bundle the CLI would write)
# ---------------------------------------------------------------------------


def test_repo_bootstrap_status_tool_inspects_existing_state(repo, monkeypatch):
    """repo_bootstrap_status reports what would be written without writing."""
    from harness import server

    monkeypatch.chdir(repo)
    # Empty repo: nothing present, the tool reports the gaps.
    status = server.repo_bootstrap_status()
    assert status["ok"]
    assert status["root"] == str(repo)
    assert status["present"]["mcp_json"] is False
    assert status["present"]["agent_tree"] is False
    assert status["present"]["opencode_json"] is False
    assert status["present"]["agents_md"] is False


def test_repo_bootstrap_status_tool_reports_present_files(repo, monkeypatch):
    """After a full bootstrap, the status tool reports what's now present."""
    from harness import scaffold, server

    monkeypatch.chdir(repo)
    scaffold.bootstrap_repo(str(repo), target="both", agents_md="overwrite", pin="abc1234")
    status = server.repo_bootstrap_status()
    assert status["present"]["mcp_json"] is True
    assert status["present"]["agent_tree"] is True
    assert status["present"]["opencode_json"] is True
    assert status["present"]["agents_md"] is True


# ---------------------------------------------------------------------------
# repo_bootstrap MCP action tool + connect-time auto-bootstrap
# ---------------------------------------------------------------------------


def test_repo_bootstrap_tool_materializes_harness(repo, monkeypatch):
    """The repo_bootstrap tool writes agent/ and (AGENTS.md opt-out default) AGENTS.md."""
    from harness import server

    monkeypatch.chdir(repo)
    res = server.repo_bootstrap()
    assert res["ok"]
    assert (repo / "agent").is_dir()
    assert (repo / "AGENTS.md").is_file(), "AGENTS.md is opt-out — default writes it"
    assert not (repo / ".mcp.json").is_file(), ".mcp.json only with pin"


def test_repo_bootstrap_tool_pin_writes_mcp_json(repo, monkeypatch):
    """repo_bootstrap(pin=...) writes the project-pinned .mcp.json for non-CC clients."""
    from harness import server

    monkeypatch.chdir(repo)
    res = server.repo_bootstrap(pin="abc1234")
    assert res["ok"]
    assert (repo / ".mcp.json").is_file()


def test_repo_bootstrap_tool_is_idempotent(repo, monkeypatch):
    """A second repo_bootstrap call is a no-op (nothing newly created)."""
    from harness import server

    monkeypatch.chdir(repo)
    server.repo_bootstrap()
    res = server.repo_bootstrap()
    assert res["ok"]
    assert "agent/" not in res["created"], "agent/ already present — must not re-create"


def test_repo_bootstrap_tool_accepts_explicit_path(repo, monkeypatch):
    """repo_bootstrap(path=...) targets that repo even when the server cwd is elsewhere.

    /new-app needs this: the server cwd is fixed at session start, but the freshly-created
    app dir is elsewhere — so the path must override the server's own repo root.
    """
    from harness import server

    monkeypatch.chdir(repo.parent)  # server cwd: not the target repo
    res = server.repo_bootstrap(path=str(repo))
    assert res["ok"]
    assert (repo / "agent").is_dir()


def test_repo_bootstrap_tool_no_repo_errors(tmp_path, monkeypatch):
    """Outside a git repo the tool returns the structured no-repo error, never writes."""
    from harness import server

    monkeypatch.chdir(tmp_path)  # tmp_path is not a git repo
    res = server.repo_bootstrap()
    assert "error" in res, "no-repo returns the structured error dict (no 'ok' key)"
    assert not (tmp_path / "agent").exists(), "must not write outside a git repo"


def test_auto_bootstrap_writes_agent_tree_and_agents_md(repo):
    """Connect-time auto-bootstrap harnesses the repo, AGENTS.md included (opt-out default)."""
    from harness import server

    server._auto_bootstrap(repo)
    assert (repo / "agent").is_dir()
    assert (repo / "AGENTS.md").is_file()


def test_auto_bootstrap_respects_agents_md_opt_out_sentinel(repo):
    """With the .harness-no-agents-md sentinel, auto-bootstrap writes agent/ but not AGENTS.md."""
    from harness import server

    (repo / server._AGENTS_MD_OPT_OUT).write_text("")
    server._auto_bootstrap(repo)
    assert (repo / "agent").is_dir(), "the harness tooling still installs"
    assert not (repo / "AGENTS.md").is_file(), "AGENTS.md suppressed by the opt-out sentinel"


def test_auto_bootstrap_fails_open_on_malformed_repo(repo):
    """A pre-existing malformed .opencode/opencode.json must not crash the server.

    _auto_bootstrap runs in the lifespan before yield, so any exception there would take
    down the whole harness server for the session. Malformed JSON raises JSONDecodeError
    (a ValueError, not OSError), so the connect-time path must swallow broadly and fail open.
    """
    from harness import server

    (repo / ".opencode").mkdir()
    (repo / ".opencode" / "opencode.json").write_text("{ this is not valid json")
    server._auto_bootstrap(repo)  # must not raise
    assert (repo / "agent").is_dir(), "the Claude-side agent/ tree still installs"


def test_repo_bootstrap_tool_surfaces_errors(repo, monkeypatch):
    """The explicit tool does NOT swallow (only the connect-time path does) — errors surface."""
    import pytest
    from harness import server

    monkeypatch.chdir(repo)
    (repo / ".opencode").mkdir()
    (repo / ".opencode" / "opencode.json").write_text("{ this is not valid json")
    with pytest.raises(ValueError):  # JSONDecodeError is a ValueError
        server.repo_bootstrap()


# ---------------------------------------------------------------------------
# opencode skills.paths convergence — the plugin rewrites the sentinel, so a
# re-bootstrap must NOT re-emit it or duplicate the resolved path (issue #5 S1)
# ---------------------------------------------------------------------------


def test_bootstrap_opencode_converges_after_plugin_rewrites_sentinel(repo):
    """Re-bootstrap after the plugin resolved skills.paths must report skipped.

    Live cycle: bootstrap writes the ``<set-by-opencode-plugin>`` sentinel; the
    opencode plugin rewrites it to a real absolute path at first load; a later
    bootstrap must converge (report ``skipped``) instead of re-adding the
    sentinel or duplicating the resolved path.
    """
    scaffold.bootstrap_repo(str(repo), target="opencode")
    oc_path = repo / ".opencode" / "opencode.json"

    # Simulate the opencode plugin rewriting the sentinel to a real path.
    cfg = json.loads(oc_path.read_text())
    cfg["skills"]["paths"] = ["/fake/abs/path"]
    oc_path.write_text(json.dumps(cfg, indent=2) + "\n")

    res = scaffold.bootstrap_repo(str(repo), target="opencode")
    after = json.loads(oc_path.read_text())
    assert after["skills"]["paths"] == ["/fake/abs/path"], "no dup, no re-added sentinel"
    assert any("opencode.json" in s for s in res["skipped"]), "must converge to skipped"


def test_bootstrap_opencode_preserves_user_skills_path(repo):
    """A user-added skills.paths entry survives a re-bootstrap."""
    scaffold.bootstrap_repo(str(repo), target="opencode")
    oc_path = repo / ".opencode" / "opencode.json"

    cfg = json.loads(oc_path.read_text())
    cfg["skills"]["paths"] = ["/my/own/skills"]
    oc_path.write_text(json.dumps(cfg, indent=2) + "\n")

    scaffold.bootstrap_repo(str(repo), target="opencode")
    after = json.loads(oc_path.read_text())
    assert "/my/own/skills" in after["skills"]["paths"], "user's entry must survive"
    assert "<set-by-opencode-plugin>" not in after["skills"]["paths"], "no sentinel re-emit"
