"""Load and evaluate the repo's agent policies (context / shell / secrets)."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ImportError:  # keep the package importable in minimal (hook) environments
    yaml = None

from repo_agent_harness import secrets as _secrets

# Conservative built-in denials — always on, even without a shell.yml.
_DENY_REGEXES = [
    (r"\brm\s+-[a-z]*r[a-z]*f|\brm\s+-[a-z]*f[a-z]*r", "Recursive force-remove is destructive."),
    (r"\bchmod\s+-R\s+777\b", "World-writable recursive chmod is unsafe."),
    (r"\bcurl\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "Piping a download into a shell is unsafe."),
    (r"\bwget\b.*\|\s*(sudo\s+)?(sh|bash|zsh)\b", "Piping a download into a shell is unsafe."),
    (r"\bfind\b.*-exec\s+cat\b", "Recursive cat wastes context; use repo.search.text / repo.read.range."),
    (r"\bgit\s+push\b.*(--force\b|-f\b)", "Force-push is destructive."),
    (r"\bdocker\b.*\bdown\b.*\s-v\b", "Removing volumes is destructive."),
]

_REQUIRE_CONFIRM = [
    (r"\bgit\s+push\b", "Pushes to a remote."),
    (r"\bgit\s+reset\s+--hard\b", "Discards local changes."),
    (r"\bgit\s+clean\s+-[a-z]*f", "Deletes untracked files."),
    (r"\bdocker\b.*\bdown\b", "Stops the stack."),
    (r"\b(alembic|prisma\s+migrate|migrate|migration)\b", "Database migration."),
]

_READERS = {"cat", "less", "more", "head", "tail", "bat", "nl", "od", "xxd", "strings"}


@dataclass
class Limits:
    """Configurable policy limits loaded from agent/policies/context.yml."""

    max_files_before_plan: int = 8
    max_lines_per_read: int = 240
    max_search_results: int = 30
    max_open_ranges_per_task: int = 20


@dataclass
class CommandCheck:
    """Result of evaluating a shell command against the repo's policies."""

    allowed: bool
    reason: str
    requires_confirmation: bool = False

    def to_dict(self) -> dict:
        """Serialise to a plain dict for JSON output."""
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "requires_confirmation": self.requires_confirmation,
        }


def _load_yaml(path: Path | None) -> dict:
    if path is None or yaml is None or not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _find_config(root: str, name: str) -> Path | None:
    """Find the highest-priority config YAML for ``name``.

    Resolution order: per-repo state dir → global harness home → package defaults.
    Returns None only if the package default is also absent (should not happen).
    """
    from repo_agent_harness.paths import harness_home, repo_id  # noqa: PLC0415

    h = harness_home()
    rid = repo_id(root)
    candidates = [
        h / "repos" / rid / "policies" / f"{name}.yml",
        h / "policies" / f"{name}.yml",
        Path(__file__).parent / "defaults" / f"{name}.yml",
    ]
    return next((p for p in candidates if p.is_file()), None)


def limits(root: str) -> Limits:
    """Load policy limits from config resolution chain, falling back to defaults."""
    p = _find_config(root, "context")
    data = _load_yaml(p) if p else {}
    lim = data.get("limits") or {}
    return Limits(
        max_files_before_plan=lim.get("max_files_before_plan", 8),
        max_lines_per_read=lim.get("max_lines_per_read", 240),
        max_search_results=lim.get("max_search_results", 30),
        max_open_ranges_per_task=lim.get("max_open_ranges_per_task", 20),
    )


def _matches(pattern: str, command: str) -> bool:
    """shell.yml entries are simple: substrings, '*' globs, or './agent/tools/*'."""
    pat = pattern.strip()
    if "*" in pat:
        rx = re.escape(pat).replace(r"\*", ".*")
        return re.search(rx, command) is not None
    return pat in command


def _read_targets(command: str) -> list[str]:
    """File arguments of read-style commands (cat/less/head/tail/...)."""
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    targets, seen_reader = [], False
    for tok in parts:
        if tok in _READERS:
            seen_reader = True
            continue
        if seen_reader and not tok.startswith("-"):
            targets.append(tok)
    return targets


def _deny_by_policy(cmd: str, deny: list[str]) -> CommandCheck | None:
    """Return a deny decision if the command matches a configured deny pattern."""
    for pat in deny:
        if _matches(pat, cmd):
            return CommandCheck(False, f"Denied by policy ('{pat}'). Prefer a repo-agent-harness tool.")
    return None


def _deny_by_builtin(cmd: str) -> CommandCheck | None:
    """Return a deny decision if the command matches a built-in deny regex."""
    for rx, why in _DENY_REGEXES:
        if re.search(rx, cmd):
            return CommandCheck(False, f"{why} Prefer a repo-agent-harness tool.")
    return None


def _deny_secret_read(cmd: str, root: str) -> CommandCheck | None:
    """Return a deny decision if the command reads a secret file path."""
    sec = _secrets.load(root)
    for tok in _read_targets(cmd):
        if _secrets.is_secret_path(tok, sec):
            return CommandCheck(False, f"Reading a secret path ('{tok}') is blocked by policy.")
    return None


def _confirm_by_policy(cmd: str, require: list[str]) -> CommandCheck | None:
    """Return a confirmation-required decision if the command matches a configured pattern."""
    for pat in require:
        if _matches(pat, cmd):
            return CommandCheck(True, f"Allowed but confirm first ('{pat}').", True)
    return None


def _confirm_by_builtin(cmd: str) -> CommandCheck | None:
    """Return a confirmation-required decision if the command matches a built-in pattern."""
    for rx, why in _REQUIRE_CONFIRM:
        if re.search(rx, cmd):
            return CommandCheck(True, f"{why} Confirm before running.", True)
    return None


def check_command(command: str, root: str) -> CommandCheck:
    """Evaluate a shell command against configured and built-in policies.

    Args:
        command: The shell command string to evaluate.
        root: Repository root directory used to load policy files.

    Returns:
        A CommandCheck indicating allowed/denied and whether confirmation is needed.
    """
    cmd = command.strip()
    if not cmd:
        return CommandCheck(True, "Empty command.")

    p = _find_config(root, "shell")
    cfg = _load_yaml(p) if p else {}
    deny_cfg: list[str] = cfg.get("deny") or []
    require_cfg: list[str] = cfg.get("require_confirmation") or []
    allow_cfg: list[str] = cfg.get("allow") or []

    for check in (
        _deny_by_policy(cmd, deny_cfg),
        _deny_by_builtin(cmd),
        _deny_secret_read(cmd, root),
        _confirm_by_policy(cmd, require_cfg),
        _confirm_by_builtin(cmd),
    ):
        if check is not None:
            return check

    for pat in allow_cfg:
        if _matches(pat, cmd):
            return CommandCheck(True, "Allowed by policy.")
    return CommandCheck(True, "No policy match; allowed (prefer harness tools for common ops).")
