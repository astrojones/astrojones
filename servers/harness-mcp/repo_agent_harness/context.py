"""Repo introspection: overview, relevant files, search, range reads."""

from __future__ import annotations

import fnmatch
import re
from pathlib import Path

import yaml

from repo_agent_harness import git, policies, shell
from repo_agent_harness import secrets as _secrets

LANG_BY_EXT = {
    ".py": "Python",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".go": "Go",
    ".rs": "Rust",
    ".rb": "Ruby",
    ".java": "Java",
    ".kt": "Kotlin",
    ".c": "C",
    ".h": "C",
    ".cpp": "C++",
    ".cc": "C++",
    ".cs": "C#",
    ".php": "PHP",
    ".swift": "Swift",
    ".sh": "Shell",
    ".scala": "Scala",
    ".dart": "Dart",
    ".ex": "Elixir",
    ".exs": "Elixir",
    ".lua": "Lua",
}

PKG_MANAGERS = {
    "pyproject.toml": "pip/uv (pyproject)",
    "requirements.txt": "pip (requirements)",
    "package.json": "npm/node",
    "pnpm-lock.yaml": "pnpm",
    "yarn.lock": "yarn",
    "package-lock.json": "npm",
    "go.mod": "Go modules",
    "Cargo.toml": "Cargo",
    "composer.json": "Composer",
    "Gemfile": "Bundler",
    "build.gradle": "Gradle",
    "pom.xml": "Maven",
}

KNOWN_TOOLS = [
    "git",
    "rg",
    "fd",
    "uv",
    "python3",
    "ruff",
    "eslint",
    "pytest",
    "mypy",
    "pyright",
    "tsc",
    "npm",
    "pnpm",
    "yarn",
    "go",
    "cargo",
]

ENTRYPOINT_CANDIDATES = [
    "main.py",
    "__main__.py",
    "app.py",
    "manage.py",
    "src/main.py",
    "src/index.ts",
    "src/index.js",
    "index.js",
    "main.go",
    "src/main.rs",
]

_BINARY_SNIFF = 4096
_RG_FIELD_COUNT = 3  # ripgrep output fields: filepath:lineno:content
_MIN_TERM_LEN = 2  # terms ≤ this length are too noisy for relevance scoring
_HIGH_CONFIDENCE_SCORE = 4  # score threshold for high-confidence file relevance


def _read_manifest(root: str) -> dict:
    p = Path(root) / "agent" / "manifest.yml"
    if p.is_file():
        return yaml.safe_load(p.read_text()) or {}
    return {}


def _top_level_dirs(rootp: Path) -> list[str]:
    out = [c.name + "/" for c in sorted(rootp.iterdir()) if c.is_dir() and not c.name.startswith(".")]
    return out[:20]


def _entry_names(d: Path, *, dirs: bool, suffix: str = "") -> list[str]:
    """Sorted names of files (stem) or dirs directly under ``d``; skips dot/underscore entries."""
    if not d.is_dir():
        return []
    out = [
        c.stem if (suffix and not dirs) else c.name
        for c in d.iterdir()
        if (c.is_dir() if dirs else c.is_file())
        and not c.name.startswith((".", "_"))
        and (c.suffix == suffix if (suffix and not dirs) else True)
    ]
    return sorted(out)


def _harness_summary(rootp: Path) -> dict:
    """Surface the repo-carried harness inventory — pulled on demand, never ambient.

    ``guide`` points at AGENTS.md only when it carries the workflow section, so a client
    knows to read it. ``agents``/``skills`` are present only in plugin checkouts; scaffolded
    target repos carry ``agent/`` (policies + tools) but no plugin dirs, and list empty.
    """
    from repo_agent_harness import scaffold

    agents_md = rootp / "AGENTS.md"
    guide = None
    if agents_md.is_file():
        text = agents_md.read_text(errors="ignore")
        if scaffold.SECTION_BEGIN in text and scaffold.SECTION_END in text:
            guide = "AGENTS.md"
    agent = rootp / "agent"
    return {
        "harnessed": guide is not None or agent.is_dir(),
        "guide": guide,
        "policies": _entry_names(agent / "policies", dirs=False, suffix=".yml"),
        "tools": _entry_names(agent / "tools", dirs=False),
        "agents": _entry_names(rootp / "agents", dirs=False, suffix=".md"),
        "skills": _entry_names(rootp / "skills", dirs=True),
    }


def overview(root: str) -> dict:
    """Summarize the repo: languages, package managers, entrypoints, important paths, available tools."""
    files = git.list_files(root)
    counts: dict[str, int] = {}
    for f in files:
        lang = LANG_BY_EXT.get(Path(f).suffix.lower())
        if lang:
            counts[lang] = counts.get(lang, 0) + 1
    languages = [lang for lang, _ in sorted(counts.items(), key=lambda kv: -kv[1])]

    rootp = Path(root)
    pkgs = [name for marker, name in PKG_MANAGERS.items() if (rootp / marker).is_file()]
    entrypoints = [c for c in ENTRYPOINT_CANDIDATES if (rootp / c).exists()]
    available = [t for t in KNOWN_TOOLS if shell.which(t)]
    manifest = _read_manifest(root)

    return {
        "name": manifest.get("name") or rootp.name,
        "root": root,
        "languages": languages,
        "frameworks": manifest.get("frameworks") or [],
        "package_managers": pkgs,
        "entrypoints": manifest.get("entrypoints") or entrypoints,
        "important_paths": manifest.get("important_paths") or _top_level_dirs(rootp),
        "available_tools": available,
        "harness": _harness_summary(rootp),
    }


def resolve_within_repo(root: str, path: str) -> Path:
    """Resolve a repo-relative or absolute path, rejecting traversal outside root."""
    rootp = Path(root).resolve()
    raw = Path(path)
    target = (raw if raw.is_absolute() else rootp / raw).resolve()
    if target != rootp and rootp not in target.parents:
        msg = f"path escapes repository root: {path}"
        raise ValueError(msg)
    return target


def _is_binary(p: Path) -> bool:
    try:
        with p.open("rb") as fh:
            return b"\x00" in fh.read(_BINARY_SNIFF)
    except OSError:
        return False


def read_range(root: str, path: str, start_line: int = 1, end_line: int = 200) -> dict:
    """Read a bounded line range from a file, with secret-redaction, binary and traversal guards."""
    target = resolve_within_repo(root, path)
    rel = str(target.relative_to(Path(root).resolve()))
    cfg = _secrets.load(root)
    if _secrets.is_secret_path(rel, cfg):
        return {"error": f"refused: '{rel}' is a secret path", "path": rel}
    if not target.is_file():
        return {"error": f"not a file: {rel}", "path": rel}
    if _is_binary(target):
        return {"error": f"refused: '{rel}' is a binary file", "path": rel}

    cap = policies.limits(root).max_lines_per_read
    start = max(1, start_line)
    end = max(start, end_line)
    capped = (end - start + 1) > cap
    if capped:
        end = start + cap - 1
    lines = target.read_text(errors="replace").splitlines()
    selected = lines[start - 1 : end]
    return {
        "path": rel,
        "start_line": start,
        "end_line": min(end, len(lines)),
        "content": _secrets.redact("\n".join(selected), cfg),
        "truncated": capped,
    }


def search_files(root: str, pattern: str, limit: int = 20) -> dict:
    """List tracked files matching a glob pattern (name or full path)."""
    files = git.ls_files(root)
    matches = [f for f in files if fnmatch.fnmatch(f, pattern) or fnmatch.fnmatch(Path(f).name, pattern)]
    return {"files": matches[:limit], "truncated": len(matches) > limit}


def search_text(root: str, pattern: str, paths: list[str] | None = None, limit: int = 20) -> dict:
    """Search file contents with ripgrep (or a Python fallback), secret-redacted."""
    cfg = _secrets.load(root)
    matches: list[dict] = []
    if shell.which("rg"):
        args = ["rg", "--line-number", "--no-heading", "--color=never", "-m", str(limit + 5), pattern]
        if paths:
            args += paths
        res = shell.run(args, cwd=root, timeout=20)
        for line in res.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < _RG_FIELD_COUNT:
                continue
            fp, ln, preview = parts
            if _secrets.is_secret_path(fp, cfg):
                continue
            matches.append(
                {
                    "path": fp,
                    "line": int(ln) if ln.isdigit() else 0,
                    "preview": _secrets.redact(preview.strip()[:200], cfg),
                }
            )
            if len(matches) >= limit:
                break
    else:
        matches = _search_text_py(root, pattern, paths, limit, cfg)
    return {"matches": matches, "truncated": len(matches) >= limit}


def _search_text_py(root, pattern, paths, limit, cfg) -> list[dict]:
    rx = re.compile(re.escape(pattern))
    out: list[dict] = []
    for f in paths or git.ls_files(root):
        if _secrets.is_secret_path(f, cfg):
            continue
        fp = Path(root) / f
        if not fp.is_file() or _is_binary(fp):
            continue
        try:
            for i, line in enumerate(fp.read_text(errors="replace").splitlines(), 1):
                if rx.search(line):
                    out.append({"path": f, "line": i, "preview": _secrets.redact(line.strip()[:200], cfg)})
                    if len(out) >= limit:
                        return out
        except OSError:
            continue
    return out


def relevant_files(root: str, task: str, max_files: int = 8) -> dict:
    """Heuristically rank tracked files by relevance to a natural-language task description."""
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9_]+", task) if len(t) > _MIN_TERM_LEN]
    manifest = _read_manifest(root)
    important = [p.rstrip("/").lower() for p in (manifest.get("important_paths") or [])]
    scored: list[tuple[int, str]] = []
    for f in git.ls_files(root):
        low = f.lower()
        stem = Path(low).stem
        score = sum(2 for t in terms if t in stem) + sum(1 for t in terms if t in low)
        if any(low.startswith(p) for p in important):
            score += 1
        if score:
            scored.append((score, f))
    scored.sort(key=lambda s: (-s[0], s[1]))
    picked = scored[:max_files]
    confidence = "high" if picked and picked[0][0] >= _HIGH_CONFIDENCE_SCORE else "medium" if picked else "low"
    return {
        "files": [{"path": f, "reason": "name/path matches task terms"} for _, f in picked],
        "confidence": confidence,
        "method": "heuristic (path/term matching) — use Serena (find_symbol / "
        "find_referencing_symbols) for symbol-level relevance",
    }
