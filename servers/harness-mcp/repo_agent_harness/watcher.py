"""In-process repo watcher: invalidate the health cache when the worktree changes.

Invalidate-only by design — no checks ever run in the background; health
snapshots refresh lazily on the next read. The watcher lives inside the
long-lived MCP server process (no separate daemon) and degrades to a no-op
when watchfiles is unavailable, in which case health relies on its TTL and
git-status staleness probe.

``.git/HEAD`` and ``.git/index`` are watched explicitly so commits and branch
switches invalidate git/ci checks even when no worktree file changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import watchfiles

from repo_agent_harness import shell

if TYPE_CHECKING:
    from collections.abc import Callable

DEBOUNCE_MS = 500
_GIT_META_SUFFIXES = ("/.git/HEAD", "/.git/index")


class _GitAwareFilter(watchfiles.DefaultFilter):
    """DefaultFilter (drops .git/, caches, editor temp files) + .git/{HEAD,index}."""

    def __call__(self, change: watchfiles.Change, path: str) -> bool:
        if path.endswith(_GIT_META_SUFFIXES):
            return True
        return bool(super().__call__(change, path))


class RepoWatcher:
    """Watch a repo root and report changed repo-relative paths to ``on_invalidate``."""

    def __init__(self, root: str, on_invalidate: Callable[[set[str]], None]) -> None:
        """Resolve ``root`` (symlinks break path mapping, e.g. /var on macOS) and store the callback."""
        self.root = str(Path(root).resolve())
        self.on_invalidate = on_invalidate
        self._stop = anyio.Event()
        self._ignored: dict[str, bool] = {}

    async def run(self) -> None:
        """Watch until ``stop()`` is called."""
        async for changes in watchfiles.awatch(
            self.root,
            debounce=DEBOUNCE_MS,
            stop_event=self._stop,
            watch_filter=_GitAwareFilter(),
        ):
            paths = self._relevant({p for _, p in changes})
            if paths:
                self.on_invalidate(paths)

    def stop(self) -> None:
        """Signal the watch loop to exit."""
        self._stop.set()

    def _relevant(self, absolute: set[str]) -> set[str]:
        """Map absolute paths to repo-relative ones, dropping gitignored files."""
        prefix = self.root.rstrip("/") + "/"
        relative = {p[len(prefix) :] for p in absolute if p.startswith(prefix)}
        meta = {p for p in relative if p.startswith(".git/")}
        return meta | self._not_ignored(relative - meta)

    def _not_ignored(self, paths: set[str]) -> set[str]:
        """Filter out gitignored paths via ``git check-ignore`` (verdicts cached)."""
        unknown = sorted(p for p in paths if p not in self._ignored)
        if unknown:
            res = shell.run(["git", "check-ignore", "--", *unknown], cwd=self.root, timeout=10)
            ignored = set(res.stdout.splitlines())
            for p in unknown:
                self._ignored[p] = p in ignored
        return {p for p in paths if not self._ignored.get(p, False)}
