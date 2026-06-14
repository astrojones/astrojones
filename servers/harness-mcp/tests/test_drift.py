"""Tests for the prompt-drift check.

The harness server is the source of truth for the per-repo workflow prompt
bodies. The plugin repo also ships ``skills/<name>/SKILL.md`` files
(under the plugin root, not under ``.claude/`` — these are the canonical
plugin-shipped skills directory) as offline copies for Claude Code users
whose client does not surface MCP prompts. These two can drift if someone
edits one but not the other; the drift check warns (never errors) when
they diverge.

The drift check has two layers:

- ``prompt_drift`` (pure function): compares a harness prompt body to an
  on-disk SKILL.md body, returns a structured result dict.
- ``check_repo_drift`` (filesystem walker): walks the plugin repo's
  ``skills/`` and reports which prompts are in sync, which have
  drifted, and which are missing entirely.
- ``repo_drift_check`` (MCP tool): the wire entry point.
- ``sync_prompts``: the operator tool to refresh the on-disk copies.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from harness import drift, prompts_registry

# ---------------------------------------------------------------------------
# compare_bodies — pure function, no I/O
# ---------------------------------------------------------------------------


def test_compare_bodies_match_returns_ok():
    """Identical bodies are in sync."""
    result = drift.compare_bodies(
        name="bugfix",
        harness_body="# Bugfix workflow\n\ndo the thing\n",
        local_body="---\nname: bugfix\n---\n# Bugfix workflow\n\ndo the thing\n",
    )
    assert result["ok"] is True
    assert result["name"] == "bugfix"
    assert result["in_sync"] is True
    assert "drift" not in result


def test_compare_bodies_drift_returns_warning():
    """A divergent local body produces a warning, not an error."""
    result = drift.compare_bodies(
        name="bugfix",
        harness_body="# Bugfix workflow\n\ndo the thing\n",
        local_body="---\nname: bugfix\n---\n# Bugfix workflow\n\ndo the thing DIFFERENTLY\n",
    )
    assert result["ok"] is True  # warnings are not errors
    assert result["in_sync"] is False
    assert result["severity"] == "warning"
    assert "drift" in result
    # The diff is a unified-diff-style hint, not the full body (keep the message short).
    assert "DIFFERENTLY" in result["drift"]


def test_compare_bodies_handles_missing_local():
    """No on-disk copy means drift can be flagged as missing."""
    result = drift.compare_bodies(
        name="newprompt",
        harness_body="some body\n",
        local_body=None,
    )
    assert result["ok"] is True
    assert result["in_sync"] is False
    assert result["severity"] == "warning"
    assert "no on-disk" in result["message"]


def test_compare_bodies_ignores_yaml_frontmatter():
    """Differences only in frontmatter are not drift."""
    result = drift.compare_bodies(
        name="bugfix",
        harness_body="# Bugfix workflow\n\nbody\n",
        local_body="---\nname: bugfix\ndescription: a different description here\n---\n# Bugfix workflow\n\nbody\n",
    )
    assert result["in_sync"] is True, "frontmatter-only diff must not be drift"


def test_compare_bodies_ignores_trailing_whitespace():
    """A trailing newline difference is not drift."""
    result = drift.compare_bodies(
        name="bugfix",
        harness_body="# Bugfix\n\nbody\n",
        local_body="---\nname: bugfix\n---\n# Bugfix\n\nbody",
    )
    assert result["in_sync"] is True, "trailing-whitespace-only diff must not be drift"


# ---------------------------------------------------------------------------
# check_repo_drift — filesystem walker
# ---------------------------------------------------------------------------


def test_check_repo_drift_all_in_sync(tmp_path, monkeypatch):
    """When every plugin SKILL.md matches the harness body, the result is in_sync everywhere."""
    for name in prompts_registry.list_names():
        entry = prompts_registry.get(name)
        assert entry is not None
        skill_dir = tmp_path / "skills" / name
        skill_dir.mkdir(parents=True)
        # Copy the harness body verbatim, wrap in frontmatter.
        (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n{entry.body}")

    result = drift.check_repo_drift(tmp_path)
    assert result["ok"] is True
    assert result["root"] == str(tmp_path)
    assert result["checked"] == len(prompts_registry.list_names())
    assert result["in_sync"] == len(prompts_registry.list_names())
    assert result["drifted"] == []
    assert result["missing"] == []


def test_check_repo_drift_detects_drifted_skill(tmp_path):
    """A divergent local SKILL.md is reported as drifted (warning)."""
    entry = prompts_registry.get("bugfix")
    assert entry is not None
    skill_dir = tmp_path / "skills" / "bugfix"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: bugfix\n---\n{entry.body}LOCAL_EDIT\n")

    result = drift.check_repo_drift(tmp_path)
    assert result["ok"] is True  # warning, not error
    assert "bugfix" in result["drifted"]
    # Other prompts are missing (no on-disk file at all), not drifted.
    assert "bugfix" not in result["missing"]


def test_check_repo_drift_detects_missing_skill(tmp_path):
    """A prompt with no on-disk file is reported as missing (info, not warning)."""
    # Don't write any SKILL.md files.
    result = drift.check_repo_drift(tmp_path)
    # Missing is not drift; result is still ok=True and the in_sync count is zero.
    assert result["ok"] is True
    assert result["in_sync"] == 0
    assert set(result["missing"]) == set(prompts_registry.list_names())


def test_check_repo_drift_handles_no_skills_dir(tmp_path):
    """A repo with no .claude/skills/ at all reports every prompt as missing."""
    result = drift.check_repo_drift(tmp_path)
    assert result["ok"] is True
    assert result["checked"] == len(prompts_registry.list_names())
    assert result["missing"] == sorted(prompts_registry.list_names())


# ---------------------------------------------------------------------------
# MCP tool
# ---------------------------------------------------------------------------


def test_repo_drift_check_tool_reports_summary(tmp_path, monkeypatch):
    """The repo_drift_check MCP tool returns the same shape as check_repo_drift."""
    from harness import server

    # Walk the plugin repo's actual .claude/skills/ (most are in sync since
    # the bodies were copied verbatim from the harness server when this PR
    # was written). This is an integration-style smoke test.
    repo_root = Path(__file__).resolve().parents[3]  # servers/harness-mcp/tests -> plugin root
    skills_root = repo_root / "skills"
    if not skills_root.is_dir():
        pytest.skip("plugin repo has no skills/ dir to drift-check against")
    monkeypatch.chdir(repo_root)
    result = server.repo_drift_check()
    assert result["ok"] is True
    assert "checked" in result
    assert "in_sync" in result
    assert "drifted" in result
    assert "missing" in result
    # The plugin ships one SKILL.md per prompt, so in_sync + missing == checked.
    assert result["in_sync"] + len(result["drifted"]) + len(result["missing"]) == result["checked"]


def test_cli_drift_check_subcommand(repo, monkeypatch, capsys):
    """`repo-agent-harness drift-check` prints the same result dict as JSON.

    Runs against the bare `repo` fixture (a git repo with no skills/) —
    the result should report every prompt as missing.
    """
    import json

    from harness import cli

    monkeypatch.chdir(repo)
    code = cli.main(["drift-check"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"]
    assert "drifted" in out
    assert set(out["missing"]) == set(prompts_registry.list_names())
