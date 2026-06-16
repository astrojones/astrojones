"""Shared Serena-first gate: one predicate + messages for every code-read path.

Both the PreToolUse hook (native ``Read``) and the harness's own ``repo_read_range``
tool consult this module, so a fresh repo funnels *every* ergonomic whole-file code
read through Serena onboarding. Closing only one path is not enough: an agent that is
denied ``Read`` will reach for ``repo_read_range`` (observed in session 9e6fd520) and
skip onboarding entirely — so onboarding never runs and no ``.serena/memories/`` are
written, leaving future sessions with no persistent project memory.

Symbol navigation (serena_get_symbols_overview / serena_find_symbol) is intentionally
NOT gated here: onboarding itself explores via those tools, so gating them would
deadlock the very step we are trying to compel.

stdlib-only on purpose: the hook imports this on its hot path.
"""

from __future__ import annotations

import os
from pathlib import Path

GATE_ENV = "REPO_AGENT_HARNESS_NO_SERENA_GATE"

# Code-file extensions whose whole-file reading must go through Serena. Mirrors
# context.LANG_BY_EXT (kept local so the hot-path hook never imports the heavier module).
CODE_EXTENSIONS = frozenset(
    {
        ".py",
        ".pyi",
        ".ts",
        ".tsx",
        ".js",
        ".jsx",
        ".go",
        ".rs",
        ".rb",
        ".java",
        ".kt",
        ".c",
        ".h",
        ".cpp",
        ".cc",
        ".hpp",
        ".cs",
        ".php",
        ".swift",
        ".scala",
        ".dart",
        ".ex",
        ".exs",
        ".lua",
        ".sh",
    }
)

# Shown until the repo is onboarded — to BOTH Read and repo_read_range. Names
# serena_initial_instructions as the single required first action; that tool, on a
# not-yet-onboarded repo, surfaces Serena's own "run onboarding" directive.
UNBOARDED_MSG = (
    "Reading code is blocked until this repo is onboarded for Serena. Do this now, in order: "
    "(1) call serena_initial_instructions, then (2) complete serena_onboarding — writing the "
    "project memories it asks for. That one-time step launches symbol navigation and persists "
    "project memory for every future session. Until it is done, both Read and repo_read_range "
    f"refuse code files; symbol tools (serena_get_symbols_overview / serena_find_symbol) work. "
    f"(Set {GATE_ENV}=1 to disable this gate.)"
)

# Shown to native Read once onboarded — code reading stays routed through the harness.
BOARDED_MSG = (
    "Read is forbidden for code discovery in this repo. Navigate by symbol "
    "(serena_get_symbols_overview / serena_find_symbol), or read a precise range with "
    f"repo_read_range, instead. (Set {GATE_ENV}=1 to disable this gate.)"
)


def gate_disabled() -> bool:
    """Whether the operator has switched the gate off for this process."""
    return os.environ.get(GATE_ENV) == "1"


def is_code_file(path: str | Path) -> bool:
    """Whether ``path``'s extension marks it as code subject to the gate."""
    return Path(path).suffix.lower() in CODE_EXTENSIONS


def is_onboarded(root: Path) -> bool:
    """Whether the repo has Serena project memories beyond the scaffolded note."""
    mem_dir = root / ".serena" / "memories"
    try:
        return mem_dir.is_dir() and any(p.suffix == ".md" and p.stem != "memory_maintenance" for p in mem_dir.iterdir())
    except OSError:
        return True  # fail open: uncertainty must never block a read
