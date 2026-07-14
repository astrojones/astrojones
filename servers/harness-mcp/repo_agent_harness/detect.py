"""Config-driven toolchain detection.

Single source of truth for "what tool does this repo use for X" (typecheck, lint,
test), derived from the repo's actual *configuration* (``pyproject.toml`` ``[tool.*]``
tables for Python, config files like ``tsconfig.json`` / ``biome.json`` for JS/TS)
rather than a blind global-PATH ``which()`` race.

Each public selector returns a resolved command descriptor::

    {"label": "ty check", "argv": ["ty", "check"]}

or ``None`` when the repo has no configuration for that kind (the caller then falls
back to the historical which-race). ``argv`` already accounts for venv-local tools by
prefixing ``uv run`` when the tool is not on the global PATH but ``uv`` is.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

from repo_agent_harness import shell


def _governing_pyproject(root: str, files: list[str]) -> Path | None:
    """Return the nearest-ancestor ``pyproject.toml`` for ``files``, or root's, or None.

    Walks up from the changed files' common ancestor directory to find the closest
    ``pyproject.toml`` (handles monorepos where the manifest lives in a nested package,
    not at repo root). Falls back to ``root/pyproject.toml`` when no nearer one exists.
    """
    rootp = Path(root)
    start = _common_ancestor(rootp, files)
    cur = start
    while True:
        candidate = cur / "pyproject.toml"
        if candidate.is_file():
            return candidate
        if cur == rootp or rootp not in cur.parents:
            break
        cur = cur.parent
    fallback = rootp / "pyproject.toml"
    return fallback if fallback.is_file() else None


def _common_ancestor(rootp: Path, files: list[str]) -> Path:
    """Directory that contains all ``files`` (relative to ``rootp``); ``rootp`` if none."""
    dirs = [(rootp / f).parent for f in files]
    if not dirs:
        return rootp
    common = dirs[0]
    for d in dirs[1:]:
        while common != d and common not in d.parents:
            if common == rootp or rootp not in common.parents:
                return rootp
            common = common.parent
    return common


def _pyproject_tools(pyproject: Path) -> set[str]:
    """Return the top-level ``[tool.*]`` table names present in ``pyproject``.

    E.g. ``[tool.ty.rules]`` and ``[tool.ruff.lint]`` yield ``{"ty", "ruff"}``.
    Returns an empty set when the file is missing, unparseable, or has no tools.
    """
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    tool = data.get("tool")
    if not isinstance(tool, dict):
        return set()
    return {str(k) for k in tool}


def _runner(tool: str, config_dir: Path) -> list[str] | None:
    """Resolve how to invoke ``tool``: the target project's ``uv`` env, then global PATH.

    Prefers ``uv run --project <config_dir>`` when ``config_dir`` has a ``pyproject.toml``
    and ``uv`` is available, so venv-local tools (``ty``/``mypy``/``pytest``) run in the
    *target* repo's environment rather than the harness's own (whose venv is scrubbed from
    :func:`shell.which` anyway). Falls back to the tool on the global PATH, else ``None``.
    """
    if (config_dir / "pyproject.toml").is_file() and shell.which("uv"):
        return ["uv", "run", "--project", str(config_dir), tool]
    if shell.which(tool):
        return [tool]
    return None


def _python_cmd(root: str, files: list[str], tool: str, extra: list[str], label: str) -> dict | None:
    pyproject = _governing_pyproject(root, files)
    config_dir = pyproject.parent if pyproject else Path(root)
    argv = _runner(tool, config_dir)
    if argv is None:
        return None
    return {"label": label, "argv": [*argv, *extra]}


def python_typechecker(root: str, files: list[str]) -> dict | None:
    """Configured Python type checker (``ty``/``mypy``/``pyright``), or None."""
    pyproject = _governing_pyproject(root, files)
    tools = _pyproject_tools(pyproject) if pyproject else set()
    if "ty" in tools:
        return _python_cmd(root, files, "ty", ["check"], "ty check")
    if "mypy" in tools:
        return _python_cmd(root, files, "mypy", [], "mypy")
    if "pyright" in tools or (pyproject and (pyproject.parent / "pyrightconfig.json").is_file()):
        return _python_cmd(root, files, "pyright", [], "pyright")
    return None


def python_linter(root: str, files: list[str]) -> dict | None:
    """Configured Python linter (``ruff``/``flake8``), or None."""
    pyproject = _governing_pyproject(root, files)
    tools = _pyproject_tools(pyproject) if pyproject else set()
    if "ruff" in tools:
        return _python_cmd(root, files, "ruff", ["check"], "ruff check")
    config_dir = pyproject.parent if pyproject else Path(root)
    if "flake8" in tools or (config_dir / ".flake8").is_file():
        return _python_cmd(root, files, "flake8", [], "flake8")
    return None


def python_test(root: str, files: list[str]) -> dict | None:
    """Configured Python test runner (``pytest``), or None.

    Detected from ``[tool.pytest.ini_options]`` in the governing pyproject.
    """
    pyproject = _governing_pyproject(root, files)
    tools = _pyproject_tools(pyproject) if pyproject else set()
    if "pytest" in tools:
        return _python_cmd(root, files, "pytest", ["-q"], "pytest -q")
    return None


def _js_cmd(tool: str, extra: list[str], label: str) -> dict | None:
    argv = [tool] if shell.which(tool) else (["npx", "--no-install", tool] if shell.which("npx") else None)
    if argv is None:
        return None
    return {"label": label, "argv": [*argv, *extra]}


def js_typechecker(root: str, _files: list[str]) -> dict | None:
    """``tsc --noEmit`` when ``tsconfig.json`` is present, else None."""
    if (Path(root) / "tsconfig.json").is_file():
        return _js_cmd("tsc", ["--noEmit"], "tsc --noEmit")
    return None


def js_linter(root: str, _files: list[str]) -> dict | None:
    """Configured JS/TS linter (``biome``/``eslint``), or None."""
    rootp = Path(root)
    if (rootp / "biome.json").is_file() or (rootp / "biome.jsonc").is_file():
        return _js_cmd("biome", ["lint"], "biome lint")
    eslint_configs = (
        ".eslintrc.json",
        ".eslintrc.js",
        ".eslintrc.cjs",
        ".eslintrc.yml",
        "eslint.config.js",
        "eslint.config.mjs",
    )
    if any((rootp / c).is_file() for c in eslint_configs):
        return _js_cmd("eslint", [], "eslint")
    return None


def configured_tools(root: str) -> dict[str, str]:
    """Display-only map of the repo's configured tools for ``root``'s own pyproject.

    Returns ``{"typecheck": "ty", "lint": "ruff", "test": "pytest"}``-shaped data
    (only keys with a detected tool). No PATH requirement — this is the authoritative
    "what would this repo use" answer for the overview surface. Considers only
    ``root/pyproject.toml`` (no tree descent); returns ``{}`` when absent.
    """
    pyproject = Path(root) / "pyproject.toml"
    if not pyproject.is_file():
        return {}
    tools = _pyproject_tools(pyproject)
    out: dict[str, str] = {}
    for label, candidates in (
        ("typecheck", ("ty", "mypy", "pyright")),
        ("lint", ("ruff", "flake8")),
        ("test", ("pytest",)),
    ):
        for tool in candidates:
            if tool in tools:
                out[label] = tool
                break
    return out
