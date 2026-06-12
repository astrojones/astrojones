"""Heuristic file-impact analysis."""

from __future__ import annotations

import re
from pathlib import Path

from repo_agent_harness import git, shell

HIGH_RISK = [
    "migration",
    "migrations",
    "auth",
    "payment",
    "billing",
    "security",
    "schema",
    "/db/",
    "secret",
    "credential",
    "permission",
]


def _looks_like_test(path: str) -> bool:
    name = Path(path).name.lower()
    has_marker = "test" in name or "spec" in name
    return has_marker and (
        name.startswith("test")
        or "_test" in name
        or ".test" in name
        or ".spec" in name
        or "/tests/" in ("/" + path.lower())
    )


def _references(root: str, stem: str, exclude: str | None = None) -> list[str]:
    if not stem:
        return []
    if shell.which("rg"):
        res = shell.run(["rg", "-l", "--color=never", r"\b" + re.escape(stem) + r"\b"], cwd=root, timeout=20)
    else:
        res = shell.run(["git", "grep", "-l", "-w", stem], cwd=root, timeout=20)
    return [line for line in res.stdout.splitlines() if line.strip() and line != exclude]


def _test_files_for(root: str, stem: str) -> list[str]:
    return [f for f in git.ls_files(root) if _looks_like_test(f) and stem in Path(f).stem]


def file_impact(root: str, path: str) -> dict:
    """Return a heuristic blast-radius assessment for the given file.

    Args:
        root: Repository root directory.
        path: Repo-relative path to assess.

    Returns:
        Dict with dependents, test_targets, risk level, and notes.
    """
    stem = Path(path).stem
    refs = _references(root, stem, exclude=path)
    test_targets = sorted({f for f in refs if _looks_like_test(f)} | set(_test_files_for(root, stem)))
    dependents = [d for d in refs if not _looks_like_test(d)]

    low = path.lower()
    risk = "high" if any(k in low for k in HIGH_RISK) else "medium" if dependents else "low"
    notes = []
    if risk == "high":
        notes.append("matches a high-risk area (migrations/auth/payments/security/schema)")
    notes.append("heuristic: references found by name; confirm with Serena find_referencing_symbols")

    return {
        "path": path,
        "dependents": dependents[:30],
        "test_targets": test_targets[:30],
        "risk": risk,
        "notes": notes,
    }
