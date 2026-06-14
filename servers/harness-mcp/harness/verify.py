"""Narrow change verification: lint / typecheck / test only the files that changed.

Each kind has a core runner (auto-detects the repo's toolchain). ``verify_changed``
prefers a repo-local ``agent/tools/<kind>-changed`` script if present (so a repo can
customize), otherwise falls back to the core runner.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from harness import git, shell


def _changed_by_ext(root: str, exts: set[str]) -> list[str]:
    out = []
    for f in git.changed_files(root):
        p = Path(root) / f
        if p.suffix.lower() in exts and p.is_file():
            out.append(f)
    return out


def _tests_for(root: str, py_files: list[str]) -> list[str]:
    tracked = set(git.ls_files(root))
    tests: set[str] = set()
    for f in py_files:
        p = Path(f)
        if "test" in p.name:
            tests.add(f)
            continue
        stem = p.stem
        candidates = (
            f"test_{stem}.py",
            f"{stem}_test.py",
            f"tests/test_{stem}.py",
            str(p.parent / f"test_{stem}.py"),
        )
        tests.update(c for c in candidates if c in tracked)
    return sorted(tests)


def _skip(msg: str) -> dict:
    return {"ok": True, "skipped": True, "command": None, "output": msg}


def _result(res: shell.Result, command: str) -> dict:
    return {"ok": res.ok, "skipped": False, "command": command, "output": (res.stdout or res.stderr).strip()}


def lint_changed(root: str) -> dict:
    """Run ruff (Python) or eslint (JS/TS) against changed files only."""
    py = _changed_by_ext(root, {".py"})
    if py and shell.which("ruff"):
        return _result(
            shell.run(["ruff", "check", *py], cwd=root, timeout=120, max_chars=4000), "ruff check " + " ".join(py)
        )
    web = _changed_by_ext(root, {".js", ".jsx", ".ts", ".tsx"})
    if web and shell.which("npx"):
        return _result(
            shell.run(["npx", "--no-install", "eslint", *web], cwd=root, timeout=120, max_chars=4000),
            "npx eslint " + " ".join(web),
        )
    return _skip("no lintable changed files or no linter available")


def typecheck_changed(root: str) -> dict:
    """Run mypy, pyright, or tsc against changed files only."""
    py = _changed_by_ext(root, {".py"})
    if py and shell.which("mypy"):
        return _result(shell.run(["mypy", *py], cwd=root, timeout=180, max_chars=4000), "mypy " + " ".join(py))
    if py and shell.which("pyright"):
        return _result(shell.run(["pyright", *py], cwd=root, timeout=180, max_chars=4000), "pyright " + " ".join(py))
    web = _changed_by_ext(root, {".ts", ".tsx"})
    if web and (Path(root) / "tsconfig.json").is_file() and shell.which("npx"):
        return _result(
            shell.run(["npx", "--no-install", "tsc", "--noEmit"], cwd=root, timeout=180, max_chars=4000),
            "npx tsc --noEmit",
        )
    return _skip("no typecheckable changed files or no type checker available")


def _pytest_cmd() -> list[str]:
    """Return an available pytest invocation, falling back to sys.executable -m pytest."""
    if shell.which("pytest"):
        return ["pytest"]
    return [sys.executable, "-m", "pytest"]


def test_changed(root: str) -> dict:
    """Run pytest against the test files corresponding to changed source files."""
    py = _changed_by_ext(root, {".py"})
    tests = _tests_for(root, py)
    if tests:
        cmd = [*_pytest_cmd(), "-q", *tests]
        return _result(
            shell.run(cmd, cwd=root, timeout=300, max_chars=6000), " ".join(cmd[: -len(tests)]) + " " + " ".join(tests)
        )
    if _changed_by_ext(root, {".js", ".jsx", ".ts", ".tsx"}):
        return _skip("changed web files; run your project's `npm test -- <pattern>` for the affected area")
    return _skip("no changed-file tests detected")


_CORE = {"lint": lint_changed, "typecheck": typecheck_changed, "test": test_changed}
_SHIM = {"lint": "lint-changed", "typecheck": "typecheck-changed", "test": "test-changed"}


def _parse(out: str) -> dict:
    try:
        return json.loads(out)
    except (ValueError, TypeError):
        return {"output": out.strip()}


def run_kind(root: str, kind: str) -> dict:
    """Run a single verification kind (lint/typecheck/test) for changed files.

    Prefers a repo-local agent/tools/<kind>-changed shim when present so a repo
    can customize; otherwise falls back to the auto-detecting core runner.

    Args:
        root: Repository root directory.
        kind: One of "lint", "typecheck", or "test".

    Returns:
        Dict with kind, via, ok, skipped, command, and output.
    """
    shim = Path(root) / "agent" / "tools" / _SHIM[kind]
    if shim.is_file() and os.access(shim, os.X_OK):
        res = shell.run([str(shim), "--json"], cwd=root, timeout=300)
        payload = _parse(res.stdout)
        return {
            "kind": kind,
            "via": f"agent/tools/{_SHIM[kind]}",
            "ok": payload.get("ok", res.ok),
            "skipped": payload.get("skipped", False),
            "command": payload.get("command"),
            "output": payload.get("output", res.stdout.strip()),
        }
    return {"kind": kind, "via": "core", **_CORE[kind](root)}


def verify_changed(root: str, mode: str = "auto") -> dict:
    """Run lint, typecheck, and tests for changed files only.

    Prefers a repo-local agent/tools/<kind>-changed script when present.

    Args:
        root: Repository root directory.
        mode: Reserved for future narrowing; currently ignored.

    Returns:
        Dict with ok, results list, and summary string.
    """
    _ = mode  # reserved for future narrowing
    results, ok = [], True
    for kind in _CORE:
        entry = run_kind(root, kind)
        if not entry.get("skipped") and not entry.get("ok", True):
            ok = False
        results.append(entry)
    failed = [r["kind"] for r in results if not r.get("skipped") and not r.get("ok", True)]
    return {
        "ok": ok,
        "results": results,
        "summary": "All changed-file checks passed." if ok else f"Failed: {', '.join(failed)}.",
    }
