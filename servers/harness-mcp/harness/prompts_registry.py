"""Per-repo workflow prompts exposed by the harness MCP server.

The bodies in ``prompts/`` are the single source of truth for the workflow
prompts (bugfix, feature, refactor, test, implement, commit-semantic) and the
/harness-init workflow. They are exposed via ``@mcp.prompt`` registration so
any MCP-aware assistant that supports ``prompts/list`` discovers them, and
also via the ``repo_prompt_get`` tool wrapper for clients (opencode) that
do not surface raw prompts to the model.

The Claude plugin and the opencode plugin both derive their per-assistant
surfaces from these bodies. Drift is checked at bootstrap time and emitted
as a warning, never an error.

Adding a new prompt:

1. Drop a markdown file at ``prompts/<name>.md`` (no frontmatter needed —
   the body is the prompt).
2. Add the name to ``EXPECTED_PROMPTS`` in ``tests/test_prompts.py``. The
   test enforces the SSOT contract.

The registry is built once at import time; ``register(mcp)`` must be called
from ``server.py`` to attach the prompts to a given FastMCP instance.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastmcp import FastMCP

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptEntry:
    """A loaded prompt body plus the metadata the harness needs to serve it.

    Attributes:
        name: Prompt identifier (e.g. ``"bugfix"``). Used as the MCP prompt
            name and as the key in the registry.
        title: Human-readable title for the prompt (``prompts/list`` advertises
            it; clients can show it in UIs).
        description: When the prompt is relevant; surfaced to the model when
            the client supports ``prompts/list``.
        body: The markdown body. This is the workflow itself — assistant-
            agnostic, no per-client framing.
        source: Repo-relative path to the on-disk file, relative to the
            ``harness`` package root. Used by the drift-check
            tool to compare the served body to the on-disk file.
        checksum: SHA-256 of ``body`` (hex-encoded). Cached at load time so
            ``repo_prompt_get`` doesn't re-hash on every call.
    """

    name: str
    title: str
    description: str
    body: str
    source: str
    checksum: str


# Short descriptions are surfaced via ``prompts/list`` to help the model pick
# the right prompt. Keep them factual and trigger-words-first.
_DESCRIPTIONS: dict[str, str] = {
    "bugfix": (
        "Use when fixing a bug, diagnosing a failure, or chasing a stack "
        "trace. Guides a safe, minimal-surface fix using Serena for navigation "
        "and the harness tools for context and verification."
    ),
    "feature": (
        "Use when adding a new feature or capability. Guides a smallest-"
        "vertical-slice implementation modeled on existing patterns, with "
        "tests and targeted verification."
    ),
    "refactor": (
        "Use when restructuring or cleaning up code without changing "
        "behavior. Enforces a behavior-preserving, scope-limited refactor "
        "with impact analysis and verification."
    ),
    "test": (
        "Use when writing, repairing, or running tests. Guides narrow, "
        "targeted testing and disciplined failure triage."
    ),
    "implement": (
        "Use when taking a task from spec to done — a task file, an issue, "
        "or a clear inline description. Runs the repo's end-to-end pipeline: "
        "spec gate, plan, implement via parallel TDD agents, verify, ship."
    ),
    "commit-semantic": (
        "Use when staging and committing working-tree changes. Groups "
        "related changes into atomic conventional commits, one logical "
        "change per commit, in a build-green order."
    ),
    "harness-init": (
        "One-time bootstrap of the per-repo harness. Now automatic via "
        "``repo_bootstrap``; keep this prompt as the fallback for explicit "
        "bootstrap in environments where the MCP server is unreachable."
    ),
}

_TITLES: dict[str, str] = {
    "bugfix": "Bugfix workflow",
    "feature": "Feature workflow",
    "refactor": "Refactor workflow",
    "test": "Test workflow",
    "implement": "Spec-driven TDD pipeline",
    "commit-semantic": "Semantic commits",
    "harness-init": "Bootstrap the per-repo harness",
}


def _load_prompts() -> dict[str, PromptEntry]:
    """Read every ``prompts/<name>.md`` and build the registry.

    Missing description/title entries are tolerated (logged) so the harness
    can ship a new prompt body without a code change. The bodies, however,
    must be present — a missing body is a packaging bug that should fail
    loud at startup.
    """
    prompts_dir = files("harness") / "prompts"
    entries: dict[str, PromptEntry] = {}
    for entry in sorted(prompts_dir.iterdir(), key=lambda e: e.name):
        if entry.name.startswith("_") or not entry.name.endswith(".md"):
            continue
        name = entry.name[: -len(".md")]
        body = entry.read_text()
        if name not in _DESCRIPTIONS:
            log.warning("prompt %r has no description in prompts.py; surface it via _DESCRIPTIONS", name)
        if name not in _TITLES:
            log.warning("prompt %r has no title in prompts.py; surface it via _TITLES", name)
        entries[name] = PromptEntry(
            name=name,
            title=_TITLES.get(name, name),
            description=_DESCRIPTIONS.get(name, ""),
            body=body,
            source=f"prompts/{entry.name}",
            checksum=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        )
    return entries


# Module-level registry, built once at import. ``register`` reads from it.
REGISTRY: dict[str, PromptEntry] = _load_prompts()


def get(name: str) -> PromptEntry | None:
    """Return the entry for ``name`` or ``None`` if unknown."""
    return REGISTRY.get(name)


def list_names() -> list[str]:
    """Return the registered prompt names, sorted alphabetically."""
    return sorted(REGISTRY)


def register(mcp: FastMCP) -> None:
    """Attach every registry entry as a ``@mcp.prompt`` on ``mcp``.

    The FastMCP prompt decorator reads the function's return value as the
    prompt body. We render the body verbatim and let FastMCP wrap it in the
    standard ``[{"role": "user", "content": {"type": "text", "text": ...}}]``
    envelope via ``@mcp.prompt``.

    Args:
        mcp: The FastMCP server instance to decorate.
    """
    for name, entry in REGISTRY.items():
        # Capture-by-default: each closure binds its own ``entry``.
        def _render(_entry: PromptEntry = entry) -> str:
            return _entry.body

        _render.__name__ = f"prompt_{name.replace('-', '_')}"
        _render.__doc__ = entry.description
        mcp.prompt(name=name, description=entry.description, title=entry.title)(_render)
