"""Tests for config-driven toolchain detection (detect.py)."""

from __future__ import annotations

from pathlib import Path

import pytest
from repo_agent_harness import detect


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# --- _pyproject_tools -------------------------------------------------------


def test_pyproject_tools_reads_tool_table(tmp_path: Path):
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[tool.ty.rules]\nfoo = 'bar'\n\n[tool.ruff]\nline-length = 100\n")
    assert detect._pyproject_tools(pp) == {"ty", "ruff"}


def test_pyproject_tools_empty_when_no_tool_table(tmp_path: Path):
    pp = tmp_path / "pyproject.toml"
    pp.write_text("[project]\nname = 'x'\n")
    assert detect._pyproject_tools(pp) == set()


def test_pyproject_tools_missing_file(tmp_path: Path):
    assert detect._pyproject_tools(tmp_path / "nope.toml") == set()


# --- _governing_pyproject ---------------------------------------------------


def test_governing_pyproject_walks_up_to_nearest(tmp_path: Path):
    _write(tmp_path / "pyproject.toml", "[project]\nname='root'\n")
    nested = tmp_path / "servers" / "pkg"
    _write(nested / "pyproject.toml", "[project]\nname='nested'\n")
    got = detect._governing_pyproject(str(tmp_path), ["servers/pkg/src/mod.py"])
    assert got == nested / "pyproject.toml"


def test_governing_pyproject_falls_back_to_root(tmp_path: Path):
    _write(tmp_path / "pyproject.toml", "[project]\nname='root'\n")
    got = detect._governing_pyproject(str(tmp_path), ["src/mod.py"])
    assert got == tmp_path / "pyproject.toml"


def test_governing_pyproject_none_when_absent(tmp_path: Path):
    got = detect._governing_pyproject(str(tmp_path), ["src/mod.py"])
    assert got is None


# --- python_typechecker -----------------------------------------------------


@pytest.mark.parametrize(
    ("table", "expected_label"),
    [
        ("[tool.ty.rules]\n", "ty check"),
        ("[tool.mypy]\n", "mypy"),
        ("[tool.pyright]\n", "pyright"),
    ],
)
def test_python_typechecker_picks_configured(tmp_path: Path, monkeypatch, table, expected_label):
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n" + table)
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    cmd = detect.python_typechecker(str(tmp_path), ["mod.py"])
    assert cmd is not None
    assert cmd["label"] == expected_label


def test_python_typechecker_none_without_config(tmp_path: Path, monkeypatch):
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    assert detect.python_typechecker(str(tmp_path), ["mod.py"]) is None


# --- python_linter ----------------------------------------------------------


def test_python_linter_picks_ruff(tmp_path: Path, monkeypatch):
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n[tool.ruff]\n")
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    cmd = detect.python_linter(str(tmp_path), ["mod.py"])
    assert cmd is not None and cmd["label"] == "ruff check"


# --- _runner uv fallback ----------------------------------------------------


def test_runner_uses_global_path_when_available(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    argv = detect._runner("ty", tmp_path)
    assert argv == ["ty"]


def test_runner_falls_back_to_uv_run(tmp_path: Path, monkeypatch):
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")

    def which(tool):
        return "/usr/bin/uv" if tool == "uv" else None

    monkeypatch.setattr(detect.shell, "which", which)
    argv = detect._runner("ty", tmp_path)
    assert argv == ["uv", "run", "--project", str(tmp_path), "ty"]


def test_runner_prefers_uv_project_over_global_tool(tmp_path: Path, monkeypatch):
    """A uv project binds tools to its own env even when the bare tool is on global PATH."""
    _write(tmp_path / "pyproject.toml", "[project]\nname='x'\n")
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    argv = detect._runner("pytest", tmp_path)
    assert argv == ["uv", "run", "--project", str(tmp_path), "pytest"]


def test_runner_none_when_uninstalled_and_no_uv(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(detect.shell, "which", lambda t: None)
    assert detect._runner("ty", tmp_path) is None


# --- JS detection -----------------------------------------------------------


def test_js_linter_biome(tmp_path: Path, monkeypatch):
    _write(tmp_path / "biome.json", "{}\n")
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    cmd = detect.js_linter(str(tmp_path), ["a.ts"])
    assert cmd is not None and cmd["label"] == "biome lint"


def test_js_linter_eslint(tmp_path: Path, monkeypatch):
    _write(tmp_path / ".eslintrc.json", "{}\n")
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    cmd = detect.js_linter(str(tmp_path), ["a.ts"])
    assert cmd is not None and cmd["label"] == "eslint"


def test_js_typechecker_tsc(tmp_path: Path, monkeypatch):
    _write(tmp_path / "tsconfig.json", "{}\n")
    monkeypatch.setattr(detect.shell, "which", lambda t: "/usr/bin/" + t)
    cmd = detect.js_typechecker(str(tmp_path), ["a.ts"])
    assert cmd is not None and cmd["label"] == "tsc --noEmit"


# --- configured_tools -------------------------------------------------------


def test_configured_tools_for_own_pyproject(tmp_path: Path):
    _write(
        tmp_path / "pyproject.toml",
        "[project]\nname='x'\n[tool.ty.rules]\n[tool.ruff]\n[tool.pytest.ini_options]\n",
    )
    got = detect.configured_tools(str(tmp_path))
    assert got["typecheck"] == "ty"
    assert got["lint"] == "ruff"
    assert got["test"] == "pytest"


def test_configured_tools_empty_without_pyproject(tmp_path: Path):
    assert detect.configured_tools(str(tmp_path)) == {}
