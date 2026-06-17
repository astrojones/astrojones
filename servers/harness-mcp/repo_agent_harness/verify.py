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

from repo_agent_harness import detect, git, shell


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


def _group_by_config(root: str, files: list[str]) -> dict[Path, list[str]]:
    """Group changed files by the directory of their governing ``pyproject.toml``.

    Files whose governing config is at ``root`` (or that have none) fall into the
    ``root`` group, so single-package repos collapse to one group with paths kept
    relative to root (no behavior change).
    """
    groups: dict[Path, list[str]] = {}
    rootp = Path(root)
    for f in files:
        pp = detect._governing_pyproject(root, [f])
        config_dir = pp.parent if pp else rootp
        groups.setdefault(config_dir, []).append(f)
    return groups


def _run_grouped(root: str, files: list[str], select, timeout: int) -> dict | None:
    """Run a detected command per governing-config group; merge into one ``_result``.

    ``select(config_dir, group_files)`` (a ``detect`` selector) returns a command
    descriptor (``{"label", "argv"}``) or ``None``. If it returns ``None`` for any
    group, this returns ``None`` to signal the caller to use its which-race fallback.
    Otherwise each group runs with ``cwd`` at its governing config directory and file
    paths relativized to it; results merge as ``ok=all(ok)`` with commands/outputs
    joined.
    """
    rootp = Path(root)
    commands: list[str] = []
    outputs: list[str] = []
    ok = True
    for config_dir, group in sorted(_group_by_config(root, files).items()):
        rel = [str((rootp / f).relative_to(config_dir)) for f in group]
        cmd = select(str(config_dir), rel)
        if cmd is None:
            return None
        res = shell.run([*cmd["argv"], *rel], cwd=str(config_dir), timeout=timeout, max_chars=4000)
        ok = ok and res.ok
        commands.append(cmd["label"] + " " + " ".join(rel))
        outputs.append((res.stdout or res.stderr).strip())
    return {"ok": ok, "skipped": False, "command": " ; ".join(commands), "output": "\n".join(outputs).strip()}


def lint_changed(root: str) -> dict:
    """Lint changed files with the repo's configured linter, else the which-race fallback."""
    py = _changed_by_ext(root, {".py"})
    if py:
        detected = _run_grouped(root, py, detect.python_linter, 120)
        if detected is not None:
            return detected
        if shell.which("ruff"):
            return _result(
                shell.run(["ruff", "check", *py], cwd=root, timeout=120, max_chars=4000), "ruff check " + " ".join(py)
            )
    web = _changed_by_ext(root, {".js", ".jsx", ".ts", ".tsx"})
    if web:
        detected = _run_grouped(root, web, detect.js_linter, 120)
        if detected is not None:
            return detected
        if shell.which("npx"):
            return _result(
                shell.run(["npx", "--no-install", "eslint", *web], cwd=root, timeout=120, max_chars=4000),
                "npx eslint " + " ".join(web),
            )
    return _skip("no lintable changed files or no linter available")


def typecheck_changed(root: str) -> dict:
    """Type-check changed files with the repo's configured checker, else the which-race fallback."""
    py = _changed_by_ext(root, {".py"})
    if py:
        detected = _run_grouped(root, py, detect.python_typechecker, 180)
        if detected is not None:
            return detected
        if shell.which("mypy"):
            return _result(shell.run(["mypy", *py], cwd=root, timeout=180, max_chars=4000), "mypy " + " ".join(py))
        if shell.which("pyright"):
            return _result(
                shell.run(["pyright", *py], cwd=root, timeout=180, max_chars=4000), "pyright " + " ".join(py)
            )
    web = _changed_by_ext(root, {".ts", ".tsx"})
    if web:
        cmd = detect.js_typechecker(root, web)
        if cmd is not None:
            # tsc is whole-project: passing explicit files would bypass tsconfig.json, so don't append `web`.
            return _result(shell.run([*cmd["argv"]], cwd=root, timeout=180, max_chars=4000), cmd["label"])
    return _skip("no typecheckable changed files or no type checker available")


def _pytest_cmd() -> list[str]:
    """Return an available pytest invocation, falling back to sys.executable -m pytest."""
    if shell.which("pytest"):
        return ["pytest"]
    return [sys.executable, "-m", "pytest"]


def test_changed(root: str) -> dict:
    """Run the changed-file tests with the repo's configured runner, else the fallback."""
    py = _changed_by_ext(root, {".py"})
    tests = _tests_for(root, py)
    if tests:
        detected = _run_grouped(root, tests, detect.python_test, 300)
        if detected is not None:
            return detected
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
