"""verify.py runners consult detect first and run in the governing config dir."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from repo_agent_harness import shell, verify


def _git(args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ty_repo(tmp_path: Path) -> Path:
    """Repo whose nested package configures ty/ruff (no root pyproject)."""
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    pkg = tmp_path / "servers" / "svc"
    pkg.mkdir(parents=True)
    (pkg / "pyproject.toml").write_text("[project]\nname='svc'\n[tool.ty.rules]\n[tool.ruff]\n")
    (pkg / "mod.py").write_text("x = 1\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


class _Rec:
    """Records tool shell.run invocations; passes git commands through to the real runner."""

    def __init__(self):
        self.calls: list[dict] = []
        self._real = shell.run

    def __call__(self, argv, cwd=None, timeout=None, max_chars=None):
        if argv and argv[0] == "git":
            return self._real(argv, cwd=cwd)
        self.calls.append({"argv": list(argv), "cwd": cwd})
        return shell.Result(code=0, stdout="ok", stderr="", timed_out=False)


def test_typecheck_uses_detected_ty_in_package_dir(ty_repo, monkeypatch):
    (ty_repo / "servers" / "svc" / "mod.py").write_text("x = 2\n")
    rec = _Rec()
    monkeypatch.setattr(verify.shell, "run", rec)
    monkeypatch.setattr(verify.shell, "which", lambda t: "/usr/bin/" + t)

    out = verify.typecheck_changed(str(ty_repo))

    assert out["ok"] is True
    assert out["skipped"] is False
    assert len(rec.calls) == 1
    call = rec.calls[0]
    assert call["argv"][:2] == ["ty", "check"]
    # path relativized to the package dir
    assert "mod.py" in call["argv"]
    assert "servers/svc/mod.py" not in call["argv"]
    assert Path(call["cwd"]) == ty_repo / "servers" / "svc"
    assert "ty check" in out["command"]


def test_typecheck_falls_back_when_no_config(repo, monkeypatch):
    """Fixture pyproject has no [tool.*] -> detect returns None -> which-race fallback."""
    (repo / "src" / "payment.py").write_text("def charge():\n    return 9\n")
    rec = _Rec()
    monkeypatch.setattr(verify.shell, "run", rec)
    # mypy on PATH -> fallback which-race picks it
    monkeypatch.setattr(verify.shell, "which", lambda t: "/usr/bin/" + t if t == "mypy" else None)

    verify.typecheck_changed(str(repo))

    assert len(rec.calls) == 1
    assert rec.calls[0]["argv"][0] == "mypy"
    assert Path(rec.calls[0]["cwd"]) == repo


def test_lint_uses_detected_ruff_in_package_dir(ty_repo, monkeypatch):
    (ty_repo / "servers" / "svc" / "mod.py").write_text("x = 3\n")
    rec = _Rec()
    monkeypatch.setattr(verify.shell, "run", rec)
    monkeypatch.setattr(verify.shell, "which", lambda t: "/usr/bin/" + t)

    verify.lint_changed(str(ty_repo))

    assert len(rec.calls) == 1
    assert rec.calls[0]["argv"][:2] == ["ruff", "check"]
    assert Path(rec.calls[0]["cwd"]) == ty_repo / "servers" / "svc"


def test_typecheck_uv_fallback_when_not_on_path(ty_repo, monkeypatch):
    """Ty configured but not on global PATH -> uv run ty check, cwd in package."""
    (ty_repo / "servers" / "svc" / "mod.py").write_text("x = 4\n")
    rec = _Rec()
    monkeypatch.setattr(verify.shell, "run", rec)
    monkeypatch.setattr(verify.shell, "which", lambda t: "/usr/bin/uv" if t == "uv" else None)

    verify.typecheck_changed(str(ty_repo))

    assert rec.calls[0]["argv"][:4] == ["uv", "run", "ty", "check"]
    assert Path(rec.calls[0]["cwd"]) == ty_repo / "servers" / "svc"


@pytest.fixture
def two_pkg_repo(tmp_path: Path) -> Path:
    """Repo with two nested ty-configured packages and no root pyproject."""
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    for name in ("a", "b"):
        pkg = tmp_path / "servers" / name
        pkg.mkdir(parents=True)
        (pkg / "pyproject.toml").write_text(f"[project]\nname='{name}'\n[tool.ty.rules]\n")
        (pkg / "mod.py").write_text("x = 1\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


def test_typecheck_groups_by_governing_config_dir(two_pkg_repo, monkeypatch):
    (two_pkg_repo / "servers" / "a" / "mod.py").write_text("x = 2\n")
    (two_pkg_repo / "servers" / "b" / "mod.py").write_text("x = 2\n")
    rec = _Rec()
    monkeypatch.setattr(verify.shell, "run", rec)
    monkeypatch.setattr(verify.shell, "which", lambda t: "/usr/bin/" + t)

    out = verify.typecheck_changed(str(two_pkg_repo))

    assert out["ok"] is True
    # one invocation per governing config dir
    assert len(rec.calls) == 2
    cwds = {Path(c["cwd"]) for c in rec.calls}
    assert cwds == {two_pkg_repo / "servers" / "a", two_pkg_repo / "servers" / "b"}
    # each call's paths are relativized to its own package dir
    for call in rec.calls:
        assert call["argv"][:2] == ["ty", "check"]
        assert call["argv"][-1] == "mod.py"
    # merged command mentions both groups
    assert out["command"].count("ty check") == 2


@pytest.fixture
def ts_repo(tmp_path: Path) -> Path:
    """Repo with a tsconfig.json and one changed .ts file."""
    _git(["init", "-q"], tmp_path)
    _git(["config", "user.email", "t@t.t"], tmp_path)
    _git(["config", "user.name", "t"], tmp_path)
    (tmp_path / "tsconfig.json").write_text("{}\n")
    (tmp_path / "mod.ts").write_text("export const x = 1\n")
    _git(["add", "-A"], tmp_path)
    _git(["commit", "-qm", "init"], tmp_path)
    return tmp_path


def test_typecheck_uses_detected_tsc_without_appending_files(ts_repo, monkeypatch):
    """Tsc is whole-project: the changed file must NOT be appended (that bypasses tsconfig)."""
    (ts_repo / "mod.ts").write_text("export const x = 2\n")
    rec = _Rec()
    monkeypatch.setattr(verify.shell, "run", rec)
    monkeypatch.setattr(verify.shell, "which", lambda t: "/usr/bin/" + t)

    out = verify.typecheck_changed(str(ts_repo))

    assert out["ok"] is True
    assert out["skipped"] is False
    assert len(rec.calls) == 1
    assert rec.calls[0]["argv"] == ["tsc", "--noEmit"]
    assert "mod.ts" not in rec.calls[0]["argv"]
    assert out["command"] == "tsc --noEmit"
