"""Static symbol index: tree-sitter walk of tracked source files, persisted per worktree.

Explorers navigate from THIS index instead of launching Serena — the LSP stays reserved for
semantic operations (references, implementations, diagnostics, renames), which removes the
symbol-overview traffic from Serena's hot path (the June parallel-subagent wedges). Same
philosophy as the ``serena_tools.json`` static snapshot: precomputed structure, zero
processes at query time.

Freshness is lazy-by-mtime: every query re-parses only the files whose mtime differs from
the stored record (a tree-sitter parse is milliseconds per file), so results are always
current without a background daemon. The index persists to
``repo_state_dir(root)/symbols.json`` and doubles as the input for the Phase-3 cognee code
bridge — one indexer, two consumers.
"""

from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from repo_agent_harness import paths

if TYPE_CHECKING:
    from tree_sitter import Node

LOG = logging.getLogger(__name__)

_INDEX_NAME = "symbols.json"
_INDEX_VERSION = 3  # bump to self-heal indexes poisoned by a bad parser (e.g. the tree-sitter 0.26 ABI break)
_GIT_TIMEOUT_S = 10
_PARSE_MAX_BYTES = 1_000_000  # a tracked source file beyond 1MB is generated; skip it

# Language coverage: the repo languages (python, typescript/javascript, shell). Extend the
# two tables together when a language joins the repo.
_LANG_BY_EXT = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".sh": "bash",
    ".bash": "bash",
}
_TS_KINDS = {
    "function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "type_alias_declaration": "type",
}
_KINDS_BY_LANG: dict[str, dict[str, str]] = {
    "python": {"function_definition": "function", "class_definition": "class"},
    "typescript": _TS_KINDS,
    "tsx": _TS_KINDS,
    "javascript": {k: v for k, v in _TS_KINDS.items() if k not in {"interface_declaration", "type_alias_declaration"}},
    "bash": {"function_definition": "function"},
}


class SymbolRecord(BaseModel):
    """One named symbol in one file."""

    name: str
    kind: str
    start_line: int = Field(..., description="1-based first line of the symbol body")
    end_line: int = Field(..., description="1-based last line of the symbol body")
    parent: str | None = Field(None, description="enclosing symbol name (e.g. a method's class)")
    doc: str | None = Field(None, description="first line of the symbol's docstring, if any")


class FileSymbols(BaseModel):
    """The indexed symbols of one file, stamped with the mtime they were parsed at."""

    mtime: float
    symbols: list[SymbolRecord] = Field(default_factory=list)


class SymbolIndex(BaseModel):
    """The persisted per-worktree index."""

    version: int = _INDEX_VERSION
    generated_at: float = 0
    files: dict[str, FileSymbols] = Field(default_factory=dict)


class SymbolsOverviewIn(BaseModel):
    """Input model for repo_symbols_overview."""

    path: str | None = Field(None, description="Repo-relative file or directory prefix to scope to; None = whole repo")
    limit: int = Field(200, ge=1, le=2000, description="Maximum symbols to return")


class SymbolsOverviewResult(BaseModel):
    """repo_symbols_overview result: the (possibly scoped) symbol map."""

    root: str
    indexed_files: int
    symbols: dict[str, list[SymbolRecord]] = Field(default_factory=dict, description="path -> symbols")
    truncated: bool = False
    reparsed_files: int = Field(0, description="files re-parsed on this query (mtime drift)")


def _index_file(root: str) -> Path:
    return paths.repo_state_dir(root) / _INDEX_NAME


def _tracked_sources(root: str) -> list[str]:
    """Tracked files with an indexed-language extension (git ls-files; ignored files excluded)."""
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            ["git", "ls-files", "-z"],  # noqa: S607 - resolved from PATH like every other git call here
            cwd=root,
            capture_output=True,
            timeout=_GIT_TIMEOUT_S,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    names = proc.stdout.decode("utf-8", errors="replace").split("\0")
    return [n for n in names if n and Path(n).suffix.lower() in _LANG_BY_EXT]


def _symbol_name(node: Node) -> str | None:
    name = node.child_by_field_name("name")
    return name.text.decode("utf-8", errors="replace") if name is not None and name.text else None


def _docstring(node: Node, lang: str) -> str | None:
    """First non-empty line of the symbol's docstring (Python only), truncated to 120 chars."""
    if lang != "python":
        return None
    body = node.child_by_field_name("body")
    first = next((c for c in body.children if c.type != "comment"), None) if body else None
    # The docstring node is a `string` — either directly (newer grammar) or wrapped in an
    # `expression_statement` (older grammar). Anything else means no docstring.
    string = first.children[0] if first and first.type == "expression_statement" and first.children else first
    if string is None or string.type != "string":
        return None
    content = next((c for c in string.children if c.type == "string_content"), None)
    text = content.text.decode("utf-8", errors="replace") if content is not None and content.text else ""
    return next((s[:120] for line in text.splitlines() if (s := line.strip())), None)


def _walk(node: Node, kinds: dict[str, str], lang: str, parent: str | None, out: list[SymbolRecord]) -> None:
    next_parent = parent
    if node.type in kinds:
        name = _symbol_name(node)
        if name:
            out.append(
                SymbolRecord(
                    name=name,
                    kind=kinds[node.type],
                    start_line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    parent=parent,
                    doc=_docstring(node, lang),
                )
            )
            next_parent = name
    for child in node.children:
        _walk(child, kinds, lang, next_parent, out)


def parse_file(root: str, rel_path: str) -> list[SymbolRecord]:
    """Extract the symbol records of one file (empty on unreadable/oversized/unknown files)."""
    lang = _LANG_BY_EXT.get(Path(rel_path).suffix.lower())
    if lang is None:
        return []
    try:
        data = (Path(root) / rel_path).read_bytes()
    except OSError:
        return []
    if len(data) > _PARSE_MAX_BYTES:
        return []
    from tree_sitter_language_pack import get_parser  # noqa: PLC0415 - lazy: ~100ms import, only when indexing

    out: list[SymbolRecord] = []
    _walk(get_parser(lang).parse(data).root_node, _KINDS_BY_LANG[lang], lang, None, out)
    return out


def _load(root: str) -> SymbolIndex:
    try:
        index = SymbolIndex.model_validate_json(_index_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return SymbolIndex()
    return index if index.version == _INDEX_VERSION else SymbolIndex()


def _store(root: str, index: SymbolIndex) -> None:
    target = _index_file(root)
    tmp = target.with_name(target.name + ".tmp")
    try:
        tmp.write_text(index.model_dump_json(), encoding="utf-8")
        tmp.replace(target)
    except OSError:
        LOG.debug("symbol index write failed for %s", root, exc_info=True)


def refresh(root: str) -> tuple[SymbolIndex, int]:
    """Bring the index current (full build on first use; mtime-diff re-parse afterwards).

    Returns the fresh index and how many files were (re-)parsed.
    """
    index = _load(root)
    tracked = _tracked_sources(root)
    reparsed = 0
    fresh_files: dict[str, FileSymbols] = {}
    for rel in tracked:
        try:
            mtime = (Path(root) / rel).stat().st_mtime
        except OSError:
            continue  # racing a delete; drop from the index
        known = index.files.get(rel)
        if known is not None and known.mtime == mtime:
            fresh_files[rel] = known
            continue
        fresh_files[rel] = FileSymbols(mtime=mtime, symbols=parse_file(root, rel))
        reparsed += 1
    changed = reparsed > 0 or set(fresh_files) != set(index.files)
    index.files = fresh_files
    if changed:
        index.generated_at = time.time()
        _store(root, index)
    return index, reparsed


def overview(root: str, inp: SymbolsOverviewIn) -> SymbolsOverviewResult:
    """The (possibly path-scoped) symbol map, always current (lazy mtime refresh)."""
    index, reparsed = refresh(root)
    prefix = (inp.path or "").strip("/")
    remaining = inp.limit
    truncated = False
    out: dict[str, list[SymbolRecord]] = {}
    for rel in sorted(index.files):
        if prefix and rel != prefix and not rel.startswith(prefix + "/"):
            continue
        symbols = index.files[rel].symbols
        if not symbols:
            continue
        if remaining <= 0:
            truncated = True
            break
        take = symbols[:remaining]
        if len(take) < len(symbols):
            truncated = True
        out[rel] = take
        remaining -= len(take)
    return SymbolsOverviewResult(
        root=root,
        indexed_files=len(index.files),
        symbols=out,
        truncated=truncated,
        reparsed_files=reparsed,
    )
