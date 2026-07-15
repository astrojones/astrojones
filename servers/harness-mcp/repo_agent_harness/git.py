"""Safe, read-only git wrappers."""

from __future__ import annotations

import os

from repo_agent_harness import secrets as _secrets
from repo_agent_harness import shell


def repo_root(cwd: str | None = None) -> str | None:
    """Return the absolute repo root, or None if not inside a git repo."""
    if cwd is None:
        cwd = os.environ.get("CLAUDE_PROJECT_DIR")
    res = shell.run(["git", "rev-parse", "--show-toplevel"], cwd=cwd, timeout=10)
    if res.ok and res.stdout.strip():
        return res.stdout.strip()
    return None


def require_root(cwd: str | None = None) -> str:
    """Return the repo root or raise RuntimeError if not in a git repo."""
    root = repo_root(cwd)
    if not root:
        msg = "not inside a git repository"
        raise RuntimeError(msg)
    return root


def _porcelain(root: str) -> list[tuple[str, str]]:
    out = shell.run(["git", "status", "--porcelain"], cwd=root, timeout=15)
    rows = []
    for line in out.stdout.splitlines():
        if not line.strip():
            continue
        code, name = line[:2], line[3:]
        if " -> " in name:  # rename
            name = name.split(" -> ", 1)[1]
        rows.append((code, name))
    return rows


def status(root: str) -> dict:
    """Return branch, dirty flag, changed/untracked files, and last commit."""
    branch = shell.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=root, timeout=10)
    changed, untracked = [], []
    for code, name in _porcelain(root):
        (untracked if code.strip() == "??" else changed).append(name)
    last = shell.run(["git", "log", "-1", "--pretty=%h %s"], cwd=root, timeout=10)
    return {
        "branch": branch.stdout.strip(),
        "dirty": bool(changed or untracked),
        "changed_files": changed,
        "untracked_files": untracked,
        "last_commit": last.stdout.strip(),
    }


def head(root: str) -> str:
    """Return the short HEAD sha, or an empty string before the first commit."""
    res = shell.run(["git", "rev-parse", "--short", "HEAD"], cwd=root, timeout=10)
    return res.stdout.strip() if res.ok else ""


def ahead_behind(root: str) -> tuple[int, int] | None:
    """Return (ahead, behind) relative to the upstream branch, or None when no upstream is set."""
    res = shell.run(["git", "rev-list", "--left-right", "--count", "HEAD...@{upstream}"], cwd=root, timeout=10)
    if not res.ok:
        return None
    try:
        ahead, behind = (int(part) for part in res.stdout.split())
    except ValueError:
        return None
    return ahead, behind


def conflicted_files(root: str) -> list[str]:
    """Return files currently in a merge-conflict state."""
    return [name for code, name in _porcelain(root) if "U" in code or code in {"AA", "DD"}]


def changed_files(root: str, include_untracked: bool = True) -> list[str]:
    """Return a list of files with uncommitted changes (and optionally untracked)."""
    files = []
    for code, name in _porcelain(root):
        if code.strip() == "??" and not include_untracked:
            continue
        files.append(name)
    return files


def ls_files(root: str, pattern: str | None = None) -> list[str]:
    """List git-tracked files, optionally filtered by a path pattern.

    Recurses into initialized submodules so their files index like any other path.
    """
    args = ["git", "ls-files", "--recurse-submodules"]
    if pattern:
        args += ["--", pattern]
    out = shell.run(args, cwd=root, timeout=20)
    return [line for line in out.stdout.splitlines() if line.strip()]


def list_files(root: str, include_untracked: bool = True) -> list[str]:
    """Tracked files plus, by default, untracked-but-not-ignored files.

    Used for introspection (e.g. language detection) so a freshly scaffolded or
    mid-work repo still reports accurately, not just its committed state.
    Tracked files recurse into initialized submodules; the untracked listing runs
    separately because --recurse-submodules and --others are mutually exclusive.
    """
    out = shell.run(["git", "ls-files", "--cached", "--recurse-submodules"], cwd=root, timeout=20)
    names = {line for line in out.stdout.splitlines() if line.strip()}
    if include_untracked:
        extra = shell.run(["git", "ls-files", "--others", "--exclude-standard"], cwd=root, timeout=20)
        names |= {line for line in extra.stdout.splitlines() if line.strip()}
    return sorted(names)


def diff_current(root: str, context_lines: int = 3, max_chars: int = 20_000) -> dict:
    """Return the current uncommitted diff stat + unified, secret-redacted and truncated."""
    stat = shell.run(["git", "diff", "--stat"], cwd=root, timeout=20)
    unified = shell.run(["git", "diff", f"--unified={context_lines}"], cwd=root, timeout=20, max_chars=max_chars)
    cfg = _secrets.load(root)
    return {
        "files": [line for line in stat.stdout.splitlines() if line.strip()],
        "diff": _secrets.redact(unified.stdout, cfg),
        "truncated": len(unified.stdout) >= max_chars,
    }
