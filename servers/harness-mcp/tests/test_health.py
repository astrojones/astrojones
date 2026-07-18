"""Tests for the declarative repo-health subsystem (health.py)."""

import itertools
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from repo_agent_harness import health, paths, shell
from repo_agent_harness.models import CheckResult
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


# ------------------------------------------------------------------- in-flight surfacing


class _FakeInFlightGateway:
    """Stand-in gateway exposing the in_flight_snapshot surface (issue #26)."""

    def __init__(self, entries):
        self._entries = entries

    def in_flight_snapshot(self):
        return list(self._entries)

    def call_from_thread(self, name: str, arguments: dict) -> object:
        """Satisfies DiagnosticsGateway; the cheap test config never runs diagnostics."""
        msg = "diagnostics not exercised by this fake"
        raise NotImplementedError(msg)


def test_in_flight_surfaces_stalled_call(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    gw = _FakeInFlightGateway([{"tool": "serena_find_symbol", "cwd": str(repo), "elapsed_s": 130.0, "stalled": True}])
    snap = health.run(str(repo), gateway=gw)
    (call,) = snap.in_flight
    assert call.tool == "serena_find_symbol"
    assert call.cwd == str(repo)
    assert call.elapsed_s == pytest.approx(130.0)
    assert call.stalled is True


def test_in_flight_empty_when_gateway_reports_none(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    snap = health.run(str(repo), gateway=_FakeInFlightGateway([]))
    assert snap.in_flight == []


def test_in_flight_empty_without_gateway(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    snap = health.run(str(repo))
    assert snap.in_flight == []


# ------------------------------------------------------------------- hook heartbeats


def test_health_lists_all_hook_events_with_never_explicit(repo, isolated_harness_home):
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    snap = health.run(str(repo))
    beats = {b.event: b for b in snap.hook_heartbeats}
    assert set(beats) == set(paths.HOOK_EVENTS)
    assert all(b.last_success_at is None and b.age_s is None and b.count == 0 for b in beats.values())
    assert snap.ok is True  # heartbeats are informational — they must never flip ok


def test_health_hook_heartbeat_reflects_stamp_and_job(repo, isolated_harness_home):
    paths.stamp_hook_heartbeat(str(repo), "post-tool-use")
    paths.stamp_hook_heartbeat(str(repo), "memify")  # async jobs ride along in the same list
    _write_config(repo, CHEAP_CONFIG, isolated_harness_home)
    snap = health.run(str(repo))
    beats = {b.event: b for b in snap.hook_heartbeats}
    assert set(paths.HOOK_EVENTS) <= set(beats)
    assert beats["post-tool-use"].last_success_at is not None
    assert beats["post-tool-use"].age_s is not None
    assert beats["post-tool-use"].age_s >= 0
    assert beats["post-tool-use"].count >= 1
    assert beats["memify"].last_success_at is not None
    assert beats["pre-tool-use"].last_success_at is None  # never ran stays explicit
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


# ------------------------------------------------------------------- concurrency


@pytest.mark.timeout(30)
def test_concurrent_run_executes_checks_once(repo, monkeypatch):
    """Concurrent run() callers must share one check-suite pass, not stampede it."""
    health._CACHE.clear()
    health._CACHE_LOCKS.clear()
    expected = len([c for c in health.load_config(str(repo)).checks if c.enabled])
    assert expected >= 1, "the default config must enable at least one check"

    counter = itertools.count()
    seen = threading.Lock()
    runs = []

    def slow_check(_root, cfg, _gateway):
        with seen:
            runs.append(next(counter))
        time.sleep(0.05)  # widen the race window so siblings overlap
        return CheckResult(id=cfg.id, kind=cfg.kind)

    monkeypatch.setattr(health, "_run_check", slow_check)

    with ThreadPoolExecutor(max_workers=8) as pool:
        snapshots = [f.result() for f in [pool.submit(health.run, str(repo)) for _ in range(8)]]

    assert len(runs) == expected, "the check suite must run exactly once across all callers"
    assert all(s.ok is True for s in snapshots)
    assert len({s.ok for s in snapshots}) == 1


def test_invalidate_does_not_deadlock_with_run(repo, monkeypatch):
    """invalidate() stays lock-free, so interleaving it with run() never deadlocks."""
    health._CACHE.clear()
    health._CACHE_LOCKS.clear()

    def quick_check(_root, cfg, _gateway):
        health.invalidate(str(repo))  # interleave invalidation from the locked section
        return CheckResult(id=cfg.id, kind=cfg.kind)

    monkeypatch.setattr(health, "_run_check", quick_check)
    health.run(str(repo))  # populate the cache so invalidate has an entry to flip
    snap = health.run(str(repo))  # second pass interleaves invalidate without deadlocking
    assert snap is not None
    assert snap.ok is True
