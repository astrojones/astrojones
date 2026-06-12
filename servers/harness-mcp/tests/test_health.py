"""Tests for the declarative repo-health subsystem (health.py)."""

import json
from pathlib import Path

from repo_agent_harness import health, shell
from repo_agent_harness.paths import repo_id

CHEAP_CONFIG = """\
version: 1
checks:
  - id: worktree
    kind: git
"""


# NOTE: no cache-clearing fixture needed — every test uses a unique tmp_path repo,
# so health._CACHE entries (keyed by repo root) never collide across tests.


def _write_config(repo: Path, body: str, harness_home: Path) -> None:
    d = harness_home / "repos" / repo_id(str(repo))
    d.mkdir(parents=True, exist_ok=True)
    (d / "health.yml").write_text(body)


# ----------------------------------------------------------------------------- config


def test_defaults_when_no_config(repo):
    cfg = health.load_config(str(repo))
    ids = {c.id: c for c in cfg.checks}
    assert set(ids) == {"lint", "typecheck", "tests", "worktree", "diagnostics", "ci"}
    assert ids["ci"].enabled is False, "the network check must be opt-in"
    assert cfg.config_error is None


def test_invalid_config_falls_back_to_defaults(repo, isolated_harness_home):
    _write_config(repo, "checks: [", isolated_harness_home)
    cfg = health.load_config(str(repo))
    assert cfg.config_error is not None
    assert {c.id for c in cfg.checks} == {"lint", "typecheck", "tests", "worktree", "diagnostics", "ci"}
    snap = health.run(str(repo), only="worktree")
    assert snap.config_error is not None


def test_config_selects_checks(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    snap = health.run(str(repo))
    assert [c.id for c in snap.checks] == ["worktree"]


# ----------------------------------------------------------------------------- checks


def test_git_check_clean_repo(repo):
    snap = health.run(str(repo), only="worktree")
    (check,) = snap.checks
    assert check.ok is True
    assert "branch" in check.summary
    assert "clean" in check.summary
    assert snap.ok is True
    assert snap.git_head


def test_command_check_pass_and_fail(repo, isolated_harness_home):
    _write_config(
        repo,
        "checks:\n"
        "  - id: passing\n    kind: command\n    command: ['true']\n"
        "  - id: failing\n    kind: command\n    command: ['false']\n",
        isolated_harness_home,
    )
    snap = health.run(str(repo))
    by_id = {c.id: c for c in snap.checks}
    assert by_id["passing"].ok is True
    assert by_id["failing"].ok is False
    assert snap.ok is False


def test_command_check_policy_denied(repo, isolated_harness_home):
    _write_config(
        repo, "checks:\n  - id: bad\n    kind: command\n    command: ['rm', '-rf', '/tmp/x']\n", isolated_harness_home
    )
    snap = health.run(str(repo))
    (check,) = snap.checks
    assert check.skipped is True
    assert check.ok is None
    assert "policy" in check.summary
    assert snap.ok is True, "a refused check must not run and must not fail the snapshot"


def test_command_check_confirmation_required_is_refused(repo, isolated_harness_home):
    _write_config(
        repo, "checks:\n  - id: push\n    kind: command\n    command: ['git', 'push']\n", isolated_harness_home
    )
    snap = health.run(str(repo))
    (check,) = snap.checks
    assert check.skipped is True
    assert "policy" in check.summary


def test_ci_check_skipped_without_gh(repo, monkeypatch, isolated_harness_home):
    monkeypatch.setattr(health.shell, "which", lambda _tool: None)
    _write_config(repo, "checks:\n  - id: ci\n    kind: ci\n    branch: main\n", isolated_harness_home)
    snap = health.run(str(repo))
    (check,) = snap.checks
    assert check.skipped is True
    assert "gh" in check.summary


def test_ci_check_success(repo, monkeypatch, isolated_harness_home):
    real_run = shell.run

    def fake_run(cmd, cwd=None, timeout=shell.DEFAULT_TIMEOUT, max_chars=shell.MAX_OUTPUT_CHARS):
        if cmd[0] == "gh":
            payload = [{"status": "completed", "conclusion": "success", "headSha": "abc1234def"}]
            return shell.Result(0, json.dumps(payload), "", False)
        return real_run(cmd, cwd=cwd, timeout=timeout, max_chars=max_chars)

    monkeypatch.setattr(health.shell, "which", lambda tool: f"/usr/bin/{tool}")
    monkeypatch.setattr(health.shell, "run", fake_run)
    _write_config(repo, "checks:\n  - id: ci\n    kind: ci\n    branch: main\n", isolated_harness_home)
    snap = health.run(str(repo))
    (check,) = snap.checks
    assert check.ok is True
    assert "success" in check.summary
    assert "abc1234" in check.summary


def test_diagnostics_skips_without_gateway(repo, isolated_harness_home):
    _write_config(repo, "checks:\n  - id: diagnostics\n    kind: diagnostics\n", isolated_harness_home)
    snap = health.run(str(repo))
    (check,) = snap.checks
    assert check.skipped is True
    assert check.ok is None
    assert "gateway" in check.summary
    assert snap.ok is True


def test_verify_kinds_map_to_check_results(repo, isolated_harness_home):
    _write_config(repo, "checks:\n  - id: lint\n    kind: lint\n", isolated_harness_home)
    snap = health.run(str(repo))
    (check,) = snap.checks
    assert check.kind == "lint"
    assert "via" in check.summary
    # clean committed repo: nothing changed, so the runner skips
    assert check.skipped is True


# ------------------------------------------------------------------- cache & staleness


def test_run_caches_and_serves_cache(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    first = health.run(str(repo))
    assert first.provenance == "fresh"
    second = health.run(str(repo))
    assert second.provenance == "cache"
    assert second.stale is False


def test_worktree_change_invalidates_cache(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    health.run(str(repo))
    (repo / "src" / "payment.py").write_text("def charge():\n    return 2\n")
    snap = health.cached(str(repo))
    assert snap is not None
    assert snap.stale is True
    assert health.run(str(repo)).provenance == "fresh"


def test_invalidate_marks_dirty(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    health.run(str(repo))
    health.invalidate(str(repo), {"src/payment.py"})
    snap = health.cached(str(repo))
    assert snap is not None
    assert snap.stale is True


def test_refresh_bypasses_cache(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    health.run(str(repo))
    assert health.run(str(repo), refresh=True).provenance == "fresh"


def test_cached_returns_none_before_first_run(repo):
    assert health.cached(str(repo)) is None


def test_only_unknown_id_reports_known_ids(repo):
    snap = health.run(str(repo), only="nope")
    (check,) = snap.checks
    assert check.skipped is True
    assert "worktree" in check.summary


# -------------------------------------------------------------------------------- faces


def test_server_tool_and_resource(repo, monkeypatch, isolated_harness_home):
    from repo_agent_harness import server

    monkeypatch.chdir(repo)
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    no_snapshot = json.loads(server.res_health())
    assert "no health snapshot yet" in no_snapshot["info"]
    data = server.repo_health()
    assert data["ok"] is True
    assert data["provenance"] == "fresh"
    resource = json.loads(server.res_health())
    assert resource["provenance"] == "cache"


def test_cli_health_subcommand(repo, monkeypatch, capsys, isolated_harness_home):
    from repo_agent_harness import cli

    monkeypatch.chdir(repo)
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    code = cli.main(["health", "--check", "worktree"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True
    assert out["checks"][0]["id"] == "worktree"
