"""Tests for the invalidate-only repo watcher (watcher.py)."""

import subprocess

import anyio
import pytest
from harness import watcher

pytestmark = pytest.mark.anyio

pytest.importorskip("watchfiles")


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _wait_until(predicate, timeout: float = 5.0) -> None:
    """Poll with a deadline instead of fixed sleeps (timing-tolerant)."""
    with anyio.fail_after(timeout):
        while not predicate():
            await anyio.sleep(0.05)


async def test_tracked_change_invalidates(repo):
    hits: list[set[str]] = []
    w = watcher.RepoWatcher(str(repo), hits.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(w.run)
        await anyio.sleep(0.3)  # let awatch initialize before the write
        (repo / "src" / "payment.py").write_text("def charge():\n    return 2\n")
        await _wait_until(lambda: any("src/payment.py" in batch for batch in hits))
        w.stop()


async def test_gitignored_path_does_not_invalidate(repo):
    (repo / ".gitignore").write_text("*.log\n")
    hits: list[set[str]] = []
    w = watcher.RepoWatcher(str(repo), hits.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(w.run)
        await anyio.sleep(0.3)
        (repo / "build.log").write_text("noise\n")
        await anyio.sleep(1.5)  # > debounce; would have fired by now
        w.stop()
    assert not any("build.log" in batch for batch in hits)


async def test_branch_switch_invalidates_via_git_meta(repo):
    hits: list[set[str]] = []
    w = watcher.RepoWatcher(str(repo), hits.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(w.run)
        await anyio.sleep(0.3)
        subprocess.run(["git", "checkout", "-q", "-b", "feature"], cwd=repo, check=True, capture_output=True)
        await _wait_until(lambda: any(".git/HEAD" in batch for batch in hits))
        w.stop()


async def test_rapid_writes_all_reported(repo):
    # batch count is watchfiles' business (step-gap dependent); what matters for
    # the idempotent dirty-bit design is that every change is reported
    hits: list[set[str]] = []
    w = watcher.RepoWatcher(str(repo), hits.append)
    async with anyio.create_task_group() as tg:
        tg.start_soon(w.run)
        await anyio.sleep(0.3)
        (repo / "src" / "a.py").write_text("A = 1\n")
        (repo / "src" / "b.py").write_text("B = 2\n")
        await _wait_until(lambda: {"src/a.py", "src/b.py"} <= set().union(*hits) if hits else False)
        w.stop()


async def test_stop_terminates_run(repo):
    w = watcher.RepoWatcher(str(repo), lambda _paths: None)
    with anyio.fail_after(5):
        async with anyio.create_task_group() as tg:
            tg.start_soon(w.run)
            await anyio.sleep(0.2)
            w.stop()
    # exiting the task group without cancellation proves run() returned


async def test_server_lifespan_wires_watcher_to_health(repo, monkeypatch):
    from harness import git, health, server

    monkeypatch.chdir(repo)
    (repo / "agent").mkdir(exist_ok=True)
    (repo / "agent" / "health.yml").write_text("checks:\n  - id: worktree\n    kind: git\n")
    root = git.repo_root()
    assert root is not None
    with anyio.fail_after(10):
        async with server._lifespan(server.mcp):
            health.run(root)
            assert health._CACHE[root].dirty is False
            await anyio.sleep(0.3)
            (repo / "src" / "payment.py").write_text("def charge():\n    return 3\n")
            # the watcher's invalidate sets the dirty bit — the git-status probe can't
            await _wait_until(lambda: health._CACHE[root].dirty)
