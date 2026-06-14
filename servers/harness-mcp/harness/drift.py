"""Drift detection between the harness prompt bodies and on-disk SKILL.md copies.

The harness server is the source of truth for the per-repo workflow prompt
bodies. The plugin repo also ships ``skills/<name>/SKILL.md`` files as
offline copies for Claude Code users whose client does not surface MCP
prompts. These two can drift if someone edits one but not the other; the
drift check warns (never errors) when they diverge.

Two layers:

- :func:`compare_bodies` — pure: harness body vs. on-disk body, returns a
  structured result. No I/O.
- :func:`check_repo_drift` — walks ``<root>/skills/<name>/SKILL.md`` (the
  plugin's well-known skill directory), compares each to the harness body.
  Reports which prompts are in sync, which have drifted, and which are
  missing entirely.
- :func:`sync_prompts` — the operator tool to refresh the on-disk copies
  to match the harness. Never overwrites by default; pass ``force=True``
  to overwrite even non-drifted files.

Drift is a warning, never an error. The plugin's load-time hook calls
:func:`check_repo_drift` and emits a ``console.warn`` listing drifted
files; the user is expected to refresh via :func:`sync_prompts` when they
want the offline copy updated.
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path

from harness import prompts_registry

# SKILL.md files start with `---\n...\n---\n`. Everything after the closing
# fence is the body; everything before is YAML frontmatter (per-assistant
# configuration) and is not part of the body. We compare body-only.
_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.DOTALL)


def _strip_frontmatter(markdown: str) -> str:
    """Return the body of a SKILL.md file, with frontmatter and trailing whitespace removed."""
    stripped = _FRONTMATTER_RE.sub("", markdown, count=1)
    return stripped.strip()


def compare_bodies(
    *,
    name: str,
    harness_body: str,
    local_body: str | None,
) -> dict:
    """Compare a harness prompt body to an on-disk SKILL.md body.

    Returns a structured result dict:

    - ``ok``: True if the local body is in sync OR missing. False would mean
      an actual error condition (parse failure, etc.) — drift itself is
      never an error.
    - ``name``: prompt identifier.
    - ``in_sync``: True iff the bodies match.
    - ``severity``: ``"ok"``, ``"warning"`` (drifted), or ``"info"`` (missing).
    - ``message``: human-readable summary.
    - ``drift``: when drifted, a short unified-diff hint (truncated to
      keep the warning message readable).

    The comparison strips YAML frontmatter and ignores trailing whitespace
    so that reformatting a frontmatter ``description`` field is not flagged
    as drift.
    """
    if local_body is None:
        return {
            "ok": True,
            "name": name,
            "in_sync": False,
            "severity": "warning",
            "message": f"no on-disk SKILL.md for {name!r} (will be created on next sync_prompts)",
        }
    local = _strip_frontmatter(local_body)
    canonical = harness_body.strip()
    if local == canonical:
        return {
            "ok": True,
            "name": name,
            "in_sync": True,
            "severity": "ok",
            "message": f"{name!r} body matches the harness",
        }
    # Drift: emit a short hint, not the full diff.
    diff = "\n".join(
        difflib.unified_diff(
            canonical.splitlines(),
            local.splitlines(),
            fromfile=f"harness:{name}",
            tofile=f"local:{name}",
            lineterm="",
            n=2,
        )
    )
    return {
        "ok": True,
        "name": name,
        "in_sync": False,
        "severity": "warning",
        "message": f"{name!r} body drifted from the harness; run `sync_prompts` to refresh",
        "drift": diff,
    }


def check_repo_drift(root: str | Path) -> dict:
    """Walk ``<root>/skills/<name>/SKILL.md`` and report drift vs. the harness.

    A prompt is considered ``missing`` if no on-disk SKILL.md exists. A
    prompt is considered ``drifted`` if the file exists but the body
    diverges from the harness server. Both are warnings, never errors.

    The root does not have to be inside a git repo — this is a pure
    filesystem walk. The caller (CLI / MCP tool) chooses the root.

    Returns a dict with ``ok``, ``root``, ``checked``, ``in_sync``,
    ``drifted`` (list of names), ``missing`` (list of names), and
    ``details`` (mapping of name to the per-prompt compare result).
    """
    rootp = Path(root)
    skills_dir = rootp / "skills"
    drifted: list[str] = []
    missing: list[str] = []
    in_sync = 0
    details: dict[str, dict] = {}
    for name in prompts_registry.list_names():
        entry = prompts_registry.get(name)
        if entry is None:
            continue
        skill_file = skills_dir / name / "SKILL.md"
        local = skill_file.read_text() if skill_file.is_file() else None
        result = compare_bodies(name=name, harness_body=entry.body, local_body=local)
        details[name] = result
        if not result["in_sync"]:
            if local is None:
                missing.append(name)
            else:
                drifted.append(name)
        else:
            in_sync += 1
    return {
        "ok": True,
        "root": str(rootp),
        "skills_dir": str(skills_dir),
        "checked": len(details),
        "in_sync": in_sync,
        "drifted": sorted(drifted),
        "missing": sorted(missing),
        "details": details,
    }


def sync_prompts(root: str | Path, *, force: bool = False) -> dict:
    """Refresh the plugin's ``skills/<name>/SKILL.md`` copies to match the harness.

    Idempotent and safe by default: only writes the on-disk file when the
    body has drifted OR the file is missing. Pass ``force=True`` to
    overwrite even in-sync files (useful after a manual edit of the
    harness bodies that you want reflected everywhere).

    Each generated SKILL.md has a minimal YAML frontmatter (name +
    description) so it is consumable by any Claude-compatible skill
    loader. The body is the harness body's verbatim contents.
    """
    rootp = Path(root)
    skills_dir = rootp / "skills"
    written: list[str] = []
    skipped: list[str] = []
    overwritten: list[str] = []
    for name in prompts_registry.list_names():
        entry = prompts_registry.get(name)
        if entry is None:
            continue
        skill_dir = skills_dir / name
        dest = skill_dir / "SKILL.md"
        if dest.is_file() and not force:
            local = dest.read_text()
            cmp = compare_bodies(name=name, harness_body=entry.body, local_body=local)
            if cmp["in_sync"]:
                skipped.append(name)
                continue
        skill_dir.mkdir(parents=True, exist_ok=True)
        body_with_frontmatter = (
            f"---\n"
            f"name: {entry.name}\n"
            f"description: {entry.description}\n"
            f"---\n\n"
            f"{entry.body}"
        )
        existed = dest.is_file()
        dest.write_text(body_with_frontmatter)
        if existed:
            overwritten.append(name)
        else:
            written.append(name)
    return {
        "ok": True,
        "root": str(rootp),
        "written": sorted(written),
        "overwritten": sorted(overwritten),
        "skipped": sorted(skipped),
    }
