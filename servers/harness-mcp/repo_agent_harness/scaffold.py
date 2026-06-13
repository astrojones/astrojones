"""Install the per-repo harness template (agent/, AGENTS.md, .mcp.json) into a repository.

Backs the ``repo-agent-harness init`` and ``bootstrap`` CLI subcommands.
The packaged ``templates/`` directory is the single source of truth; this repository's
own ``agent/`` policies and tools are held byte-identical to it by a drift-guard test
(``manifest.yml`` is excluded — the live copy carries repo-specific values).

Subcommand split:

- ``init`` — narrow, opt-in: writes a project ``.mcp.json`` (with ``--pin`` /
  ``--spec``) for non-plugin environments. Skips ``agent/`` and ``AGENTS.md``
  unless explicitly requested. This is the escape-hatch subcommand.
- ``bootstrap`` — full first-touch: writes ``agent/`` (always), ``AGENTS.md``
  (with the section marker, opt-in via ``--agents-md``), ``.mcp.json`` (when
  ``--pin``/``--spec`` is set), and the opencode side (``target="opencode"|"both"``).
  The plugin's load-time hook calls this subcommand on first use.
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterator

SECTION_BEGIN = "<!-- repo-agent-harness:section:begin -->"
SECTION_END = "<!-- repo-agent-harness:section:end -->"

_REPO_URL = "https://github.com/astrojones/repo-agent-harness"
_PLACEHOLDER_NAME = "__REPO_NAME__"
_PLACEHOLDER_SPEC = "__HARNESS_SPEC__"

BootstrapTarget = Literal["claude", "opencode", "both"]

_VALID_TARGETS: frozenset[str] = frozenset({"claude", "opencode", "both"})
_CLAUDE_TARGETS: frozenset[str] = frozenset({"claude", "both"})
_OPENCODE_TARGETS: frozenset[str] = frozenset({"opencode", "both"})


def harness_spec(pin: str | None = None) -> str:
    """The uvx/pip requirement spec for the bundled MCP server, optionally sha-pinned."""
    rev = f"@{pin}" if pin else ""
    return f"git+{_REPO_URL}{rev}#subdirectory=mcp"


def _templates():
    return files("repo_agent_harness") / "templates"


def _walk(trav, prefix: str = "") -> Iterator[tuple[str, object]]:
    for entry in trav.iterdir():
        rel = f"{prefix}{entry.name}"
        if entry.is_dir():
            yield from _walk(entry, f"{rel}/")
        else:
            yield rel, entry


def _install_agent_tree(root: Path, name: str, force: bool, result: dict) -> None:
    for rel, entry in sorted(_walk(_templates() / "agent", "agent/")):
        dest = root / rel
        if dest.exists() and not force:
            result["skipped"].append(rel)
            continue
        existed = dest.exists()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(entry.read_text().replace(_PLACEHOLDER_NAME, name))
        if rel.startswith("agent/tools/"):
            dest.chmod(dest.stat().st_mode | 0o755)
        result["replaced" if existed else "created"].append(rel)


def _is_harness_installed_serena(entry: dict) -> bool:
    """True when a serena server entry matches the shape an older harness init wrote."""
    return any("github.com/oraios/serena" in str(arg) for arg in entry.get("args") or [])


def _install_mcp_json(root: Path, spec: str, result: dict) -> None:
    dest = root / ".mcp.json"
    template = json.loads((_templates() / "mcp.json").read_text().replace(_PLACEHOLDER_SPEC, spec))
    if not dest.exists():
        dest.write_text(json.dumps(template, indent=2) + "\n")
        result["created"].append(".mcp.json")
        return
    cfg = json.loads(dest.read_text())
    servers = cfg.setdefault("mcpServers", {})
    # migration: serena is proxied through the harness server now; drop the
    # standalone entry an older init installed (user-customized entries are kept)
    if "serena" in servers and _is_harness_installed_serena(servers["serena"]):
        del servers["serena"]
        result["removed"].append(".mcp.json#serena (proxied via repo-agent-harness now)")
    for key, value in template["mcpServers"].items():
        if key in servers:
            result["skipped"].append(f".mcp.json#{key}")
        else:
            servers[key] = value
            result["merged"].append(f".mcp.json#{key}")
    dest.write_text(json.dumps(cfg, indent=2) + "\n")


def _install_agents_md(root: Path, name: str, mode: str, result: dict) -> None:
    if mode == "skip":
        result["skipped"].append("AGENTS.md")
        return
    dest = root / "AGENTS.md"
    full = (_templates() / "AGENTS.md").read_text().replace(_PLACEHOLDER_NAME, name)
    section = full[full.index(SECTION_BEGIN) : full.index(SECTION_END) + len(SECTION_END)]
    if not dest.exists() or mode == "overwrite":
        existed = dest.exists()
        dest.write_text(full)
        result["replaced" if existed else "created"].append("AGENTS.md")
        return
    existing = dest.read_text()
    if SECTION_BEGIN in existing and SECTION_END in existing:
        begin = existing.index(SECTION_BEGIN)
        end = existing.index(SECTION_END) + len(SECTION_END)
        updated = existing[:begin] + section + existing[end:]
        if updated == existing:
            result["skipped"].append("AGENTS.md#section")
        else:
            result["replaced"].append("AGENTS.md#section")
    else:
        updated = existing.rstrip("\n") + "\n\n" + section + "\n"
        result["merged"].append("AGENTS.md#section")
    dest.write_text(updated)


def init_repo(
    root: str,
    *,
    agents_md: str = "skip",
    force: bool = False,
    pin: str | None = None,
    spec: str | None = None,
) -> dict:
    """Scaffold the per-repo harness into ``root``.

    Always installs ``agent/`` (policies, manifest, tools) and optionally
    ``AGENTS.md`` (controlled by ``agents_md``).  Pass ``--pin <sha>`` or
    ``--spec <spec>`` to *also* write a project ``.mcp.json`` server entry —
    an escape hatch for non-Claude-Code clients and CI environments where the
    astrojones-dev plugin is not installed.
    """
    rootp = Path(root)
    result: dict = {
        "ok": True,
        "root": str(rootp),
        "created": [],
        "merged": [],
        "replaced": [],
        "skipped": [],
        "removed": [],
    }
    _install_agent_tree(rootp, rootp.name, force, result)
    _install_agents_md(rootp, rootp.name, agents_md, result)
    if pin is not None or spec is not None:
        _install_mcp_json(rootp, spec or harness_spec(pin), result)
        result["next_steps"] = [
            "Restart the agent session so .mcp.json loads (non-Claude-Code clients).",
            "For Claude Code: the plugin bundles the harness server — no restart needed.",
        ]
    else:
        result["next_steps"] = [
            "For Claude Code: the plugin auto-connects the harness server.",
            "For non-Claude-Code clients: re-run with --pin <sha> to add .mcp.json.",
        ]
    return result


# ---------------------------------------------------------------------------
# opencode target — .opencode/opencode.json with the harness server entry and
# the per-plugin skills.paths. The block is delimited by section markers so
# existing user content above/below the block is preserved across re-runs.
# ---------------------------------------------------------------------------


def _opencode_harness_block() -> dict:
    """The harness-owned slice of ``.opencode/opencode.json``.

    Returned as a dict so the merge logic in ``_install_opencode_json`` can
    deep-merge it into an existing user file. Keys:

    - ``mcp.repo-agent-harness`` — points at the harness server (uvx spec).
    - ``mcp.repo-agent-harness.environment.HARNESS_SERVER_HOME`` — let the
      user override the harness home (default ``~/.harness``).
    - ``skills.paths`` — the bundled skills tree inside the harness server,
      so the opencode skill loader picks up the per-repo workflow prompts
      surfaced as ``SKILL.md`` by the opencode plugin's translator.
    """
    return {
        "mcp": {
            "repo-agent-harness": {
                "type": "local",
                "command": [
                    "uvx",
                    "--from",
                    harness_spec(),
                    "repo-agent-harness-mcp",
                ],
                "enabled": True,
            },
        },
        "skills": {
            "paths": [
                # Resolved at runtime by the opencode plugin: the harness
                # server's bundled prompts/<name>.md are mirrored as
                # SKILL.md files inside this path. The plugin owns the
                # actual path; this is the SSOT contract.
                "__HARNESS_OPENCODE_SKILLS_PATH__",
            ],
        },
    }


def _opencode_resolve_paths(block: dict) -> dict:
    """Replace ``__HARNESS_OPENCODE_SKILLS_PATH__`` with the real path on disk.

    The harness server ships its prompts as ``prompts/<name>.md`` inside the
    ``repo_agent_harness`` package. The opencode plugin's translator mirrors
    them as ``SKILL.md`` files at ``<plugin>/opencode/skills/<name>/SKILL.md``
    at startup. We can't know that path from the harness server alone (the
    plugin path varies by install), so the marker is a known sentinel: the
    opencode plugin rewrites it during the first-touch hook. The bare minimum
    we do here is leave the sentinel in place; the plugin replaces it.
    """
    # Shallow copy so we don't mutate the caller's dict.
    out = json.loads(json.dumps(block))
    out.setdefault("skills", {})["paths"] = [
        # The plugin rewrites this sentinel at first-touch. Until then, the
        # user can point skills.paths at the plugin's opencode/skills/ dir
        # manually if they want immediate skill discovery.
        "<set-by-opencode-plugin>",
    ]
    return out


def _drop_resolved_skills_paths(block: dict, existing: dict) -> dict:
    """Drop the ``skills.paths`` sentinel when the config already has paths.

    Returns a copy of ``block`` without ``skills.paths`` when ``existing``
    already declares non-empty ``skills.paths``.

    The sentinel (``<set-by-opencode-plugin>``) is rewritten to a real path by
    the opencode plugin. Re-merging the sentinel afterward never dedups against
    the resolved path, so the list grows each session. Dropping it lets the
    merge converge to ``skipped`` while preserving whatever paths are present.
    """
    if not existing.get("skills", {}).get("paths"):
        return block
    out = json.loads(json.dumps(block))
    skills = out.get("skills")
    if isinstance(skills, dict):
        skills.pop("paths", None)
        if not skills:
            out.pop("skills", None)
    return out


def _install_opencode_json(root: Path, force: bool, result: dict) -> None:
    """Write or merge ``.opencode/opencode.json`` with the harness wiring.

    Idempotent: if the file already exists, deep-merges the harness block
    into the existing JSON. User-added keys are preserved.
    """
    dest = root / ".opencode" / "opencode.json"
    block = _opencode_resolve_paths(_opencode_harness_block())
    if not dest.exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(json.dumps(block, indent=2) + "\n")
        result["created"].append(".opencode/opencode.json")
        return
    existing = json.loads(dest.read_text())
    # The opencode plugin rewrites the skills.paths sentinel to a real path at
    # first load. Once paths are populated, re-emitting the sentinel would
    # never dedup against the resolved path (sentinel != abs path), so the list
    # would grow every session. Drop skills.paths from the overlay when the
    # existing config already declares them — let the resolved/user paths stand.
    overlay = _drop_resolved_skills_paths(block, existing)
    if force:
        # Replace: keep user keys, overwrite the harness block.
        merged = _deep_merge(existing, overlay)
        dest.write_text(json.dumps(merged, indent=2) + "\n")
        result["replaced"].append(".opencode/opencode.json")
        return
    merged = _deep_merge(existing, overlay)
    if merged == existing:
        result["skipped"].append(".opencode/opencode.json")
    else:
        dest.write_text(json.dumps(merged, indent=2) + "\n")
        result["merged"].append(".opencode/opencode.json")


def _deep_merge(base: dict, overlay: dict) -> dict:
    """Deep-merge ``overlay`` into ``base``; ``overlay`` wins on conflict.

    Used for opencode.json so that the harness block merges into the user's
    existing config without nuking their agent definitions or skill paths.
    Dicts recurse; list-valued keys are *unioned* (order-preserving dedup) so
    a user's own ``skills.paths``/agent-list entries survive a re-bootstrap
    instead of being clobbered. Scalars are overwritten by ``overlay``.
    Returns a new dict; the inputs are not mutated.
    """
    out = dict(base)
    for k, v in overlay.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        elif k in out and isinstance(out[k], list) and isinstance(v, list):
            merged = list(out[k])
            for item in v:
                if item not in merged:
                    merged.append(item)
            out[k] = merged
        else:
            out[k] = v
    return out


def bootstrap_repo(  # noqa: PLR0913 — six kwargs are intentional; this is the public bootstrap surface
    root: str,
    *,
    target: BootstrapTarget = "claude",
    agents_md: str = "skip",
    force: bool = False,
    pin: str | None = None,
    spec: str | None = None,
) -> dict:
    """First-touch materialization of the per-repo harness.

    Different from :func:`init_repo`:

    - ``init_repo`` is a narrow opt-in: writes ``agent/`` and (with opt-in)
      ``AGENTS.md``; writes ``.mcp.json`` only with ``--pin``/``--spec``.
    - ``bootstrap_repo`` is the full first-touch: always writes ``agent/``,
      opt-in writes ``AGENTS.md`` and (with ``--pin``) ``.mcp.json``, and
      additionally writes the opencode side when ``target="opencode"|"both"``.

    Idempotent: re-running against a bootstrapped repo is a no-op. The
    opencode side deep-merges so the user's existing ``.opencode/opencode.json``
    keys are preserved.

    Args:
        root: Repo path (cwd, worktree root, or repo root).
        target: Which per-assistant surface to materialize.

            - ``"claude"`` (default) — ``.mcp.json`` (with ``--pin``),
              ``agent/``, opt-in ``AGENTS.md``.
            - ``"opencode"`` — ``.opencode/opencode.json`` (always).
            - ``"both"`` — the union.
        agents_md: ``"auto"`` append the harness section to an existing
            ``AGENTS.md``, ``"overwrite"`` replace the file, ``"skip"``
            (default) do not write.
        force: Overwrite existing harness-managed files.
        pin: Commit SHA to pin the harness spec in ``.mcp.json``.
        spec: Override the full harness requirement spec.

    Returns:
        The standard result dict with ``ok``, ``root``, ``created``,
        ``merged``, ``replaced``, ``skipped``, ``removed``, and
        ``next_steps`` (only when ``ok``).
    """
    if target not in _VALID_TARGETS:
        return {
            "ok": False,
            "root": str(root),
            "error": f"unknown target: {target!r}; expected one of: claude, opencode, both",
        }
    rootp = Path(root)
    result: dict = {
        "ok": True,
        "root": str(rootp),
        "created": [],
        "merged": [],
        "replaced": [],
        "skipped": [],
        "removed": [],
    }
    # Claude side
    if target in _CLAUDE_TARGETS:
        _install_agent_tree(rootp, rootp.name, force, result)
        _install_agents_md(rootp, rootp.name, agents_md, result)
        if pin is not None or spec is not None:
            _install_mcp_json(rootp, spec or harness_spec(pin), result)
    # opencode side
    if target in _OPENCODE_TARGETS:
        _install_opencode_json(rootp, force, result)
    # Next-steps
    next_steps: list[str] = []
    if target in _CLAUDE_TARGETS:
        if pin is not None or spec is not None:
            next_steps.append("Restart the agent session so .mcp.json loads (non-plugin clients).")
        next_steps.append("For Claude Code: the plugin auto-connects the harness server.")
    if target in _OPENCODE_TARGETS:
        next_steps.append("For opencode: the opencode plugin rewrites the skills.paths sentinel at first load.")
    result["next_steps"] = next_steps
    return result


def inspect_bootstrap(root: str) -> dict:
    """Report which per-repo harness files are already present.

    Read-only — does not write anything. Used by the MCP ``repo_bootstrap_status``
    tool and by humans debugging "is the harness installed here?".

    Returns a dict with ``ok`` and ``present`` (mapping of file to bool).
    """
    rootp = Path(root)
    present = {
        "mcp_json": (rootp / ".mcp.json").is_file(),
        "agent_tree": (rootp / "agent").is_dir(),
        "agents_md": (rootp / "AGENTS.md").is_file(),
        "opencode_json": (rootp / ".opencode" / "opencode.json").is_file(),
    }
    return {"ok": True, "root": str(rootp), "present": present}
