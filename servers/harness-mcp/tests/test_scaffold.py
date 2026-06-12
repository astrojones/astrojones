"""Tests for scaffold.init_repo — the minimal .mcp.json escape-hatch installer."""

import json
from pathlib import Path

from repo_agent_harness import scaffold

REPO_ROOT = Path(__file__).resolve().parents[2]


def _mcp_config(repo: Path) -> dict:
    return json.loads((repo / ".mcp.json").read_text())


# ---------------------------------------------------------------------------
# New behaviour: default init writes ONLY .mcp.json
# ---------------------------------------------------------------------------


def test_init_default_creates_only_mcp_json(repo):
    """Default init writes ONLY .mcp.json — no agent/ tree, no AGENTS.md."""
    res = scaffold.init_repo(str(repo))
    assert res["ok"]
    assert (repo / ".mcp.json").is_file()
    assert not (repo / "agent").exists()
    assert not (repo / "AGENTS.md").is_file()


def test_init_agents_md_overwrite_creates_agents_md(repo):
    """agents_md='overwrite' additionally creates AGENTS.md."""
    res = scaffold.init_repo(str(repo), agents_md="overwrite")
    assert res["ok"]
    assert (repo / "AGENTS.md").is_file()
    assert (repo / ".mcp.json").is_file()
    assert not (repo / "agent").exists()


def test_init_no_force_skips_existing_mcp_json_entry(repo):
    """Without --force, existing .mcp.json entries are not overwritten."""
    scaffold.init_repo(str(repo))
    res2 = scaffold.init_repo(str(repo))
    assert res2["ok"]
    # harness entry already exists; should be in skipped or merged
    assert any("repo-agent-harness" in s for s in res2.get("skipped", []) + res2.get("merged", []))


def test_init_pin_embeds_sha_in_mcp_json(repo):
    """Init --pin embeds the given SHA in the .mcp.json harness spec."""
    sha = "abc123def456" * 3
    scaffold.init_repo(str(repo), pin=sha[:40])
    mcp = json.loads((repo / ".mcp.json").read_text())
    spec_str = json.dumps(mcp)
    assert sha[:40] in spec_str


# ---------------------------------------------------------------------------
# .mcp.json merge/pin behaviour (unchanged)
# ---------------------------------------------------------------------------


def test_init_writes_mcp_json_with_single_server(repo):
    scaffold.init_repo(str(repo))
    cfg = _mcp_config(repo)
    assert set(cfg["mcpServers"]) == {"repo-agent-harness"}, "serena is proxied through the harness now"
    args = cfg["mcpServers"]["repo-agent-harness"]["args"]
    spec = args[args.index("--from") + 1]
    assert spec.startswith("git+https://")
    assert spec.endswith("#subdirectory=mcp")
    assert "__HARNESS_SPEC__" not in json.dumps(cfg)


def test_init_pin_is_written_into_spec(repo):
    scaffold.init_repo(str(repo), pin="abc1234")
    args = _mcp_config(repo)["mcpServers"]["repo-agent-harness"]["args"]
    spec = args[args.index("--from") + 1]
    assert "@abc1234#subdirectory=mcp" in spec


def test_init_spec_override_wins(repo):
    custom = "git+file:///somewhere/repo-agent-harness#subdirectory=mcp"
    scaffold.init_repo(str(repo), spec=custom)
    args = _mcp_config(repo)["mcpServers"]["repo-agent-harness"]["args"]
    assert custom in args


def test_init_merges_existing_mcp_json(repo):
    (repo / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"other": {"command": "x"}, "serena": {"command": "custom"}}})
    )
    res = scaffold.init_repo(str(repo))
    cfg = _mcp_config(repo)
    assert cfg["mcpServers"]["other"] == {"command": "x"}
    assert cfg["mcpServers"]["serena"] == {"command": "custom"}, "existing entry must not be overwritten"
    assert "repo-agent-harness" in cfg["mcpServers"]
    assert any("repo-agent-harness" in m for m in res["merged"])


def test_init_removes_harness_installed_serena_entry(repo):
    old_entry = {
        "command": "uvx",
        "args": ["--from", "git+https://github.com/oraios/serena@abc123", "serena", "start-mcp-server"],
    }
    (repo / ".mcp.json").write_text(json.dumps({"mcpServers": {"serena": old_entry}}))
    res = scaffold.init_repo(str(repo))
    cfg = _mcp_config(repo)
    assert "serena" not in cfg["mcpServers"]
    assert any("serena" in r for r in res["removed"])


# ---------------------------------------------------------------------------
# AGENTS.md section behaviour (opt-in only via agents_md="overwrite"/"auto")
# ---------------------------------------------------------------------------


def test_init_appends_section_to_existing_agents_md(repo):
    (repo / "AGENTS.md").write_text("# My charter\n\nHouse rules.\n")
    scaffold.init_repo(str(repo), agents_md="auto")
    text = (repo / "AGENTS.md").read_text()
    assert text.startswith("# My charter")
    assert "House rules." in text
    assert scaffold.SECTION_BEGIN in text
    assert scaffold.SECTION_END in text


def test_init_section_refresh_is_idempotent(repo):
    scaffold.init_repo(str(repo), agents_md="overwrite")
    first = (repo / "AGENTS.md").read_text()
    scaffold.init_repo(str(repo), agents_md="overwrite")
    second = (repo / "AGENTS.md").read_text()
    assert first == second
    assert second.count(scaffold.SECTION_BEGIN) == 1


def test_init_agents_md_skip_mode(repo):
    scaffold.init_repo(str(repo), agents_md="skip")
    assert not (repo / "AGENTS.md").exists()


# ---------------------------------------------------------------------------
# CLI smoke test
# ---------------------------------------------------------------------------


def test_cli_init_subcommand(repo, monkeypatch, capsys):
    from repo_agent_harness import cli

    monkeypatch.chdir(repo)
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    code = cli.main(["init", "--pin", "deadbee", "--json"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"]
    assert (repo / ".mcp.json").is_file()
