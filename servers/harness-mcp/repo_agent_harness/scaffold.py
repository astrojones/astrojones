"""Install the per-repo harness template (agent/, AGENTS.md, .mcp.json) into a repository.

Backs the ``repo-agent-harness init`` CLI subcommand and the plugin's /init command.
The packaged ``templates/`` directory is the single source of truth; this repository's
own ``agent/`` policies and tools are held byte-identical to it by a drift-guard test
(``manifest.yml`` is excluded — the live copy carries repo-specific values).
"""

from __future__ import annotations

import json
from importlib.resources import files
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

SECTION_BEGIN = "<!-- repo-agent-harness:section:begin -->"
SECTION_END = "<!-- repo-agent-harness:section:end -->"

_REPO_URL = "https://github.com/astrojones/repo-agent-harness"
_PLACEHOLDER_NAME = "__REPO_NAME__"
_PLACEHOLDER_SPEC = "__HARNESS_SPEC__"


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
    """Write a pinned .mcp.json into ``root`` for non-Claude-Code clients / CI.

    This is an opt-in escape hatch. The harness MCP server is bundled in the
    astrojones-dev plugin and needs no per-repo scaffolding for normal use.

    ``agents_md="overwrite"`` additionally writes AGENTS.md (docs only, opt-in).
    ``force`` allows overwriting an existing .mcp.json entry.
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
    _install_mcp_json(rootp, spec or harness_spec(pin), result)
    if agents_md != "skip":
        _install_agents_md(rootp, rootp.name, agents_md, result)
    result["next_steps"] = [
        "Restart the agent session so .mcp.json loads (non-Claude-Code clients).",
        "For Claude Code: the plugin bundles the harness server — no init needed.",
    ]
    return result
