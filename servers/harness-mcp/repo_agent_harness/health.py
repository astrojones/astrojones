"""Configurable repository health: declarative checks over the shared core.

Checks are declared in ``agent/health.yml`` (agent-editable, like the policies)
and define what "healthy" means for the repo. Built-in kinds reuse the
verify/git cores; ``command`` runs a custom argv list (shell-policy gated);
``ci`` (opt-in, network) asks ``gh`` for the latest workflow run;
``diagnostics`` reports LSP diagnostics once the Serena gateway is available.

Snapshots are cached per repo root with provenance (fresh vs cache) and a
staleness probe over ``git status --porcelain`` so cached reads stay honest.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

try:
    import yaml
except ImportError:  # keep the package importable in minimal (hook) environments
    yaml = None

from repo_agent_harness import git, policies, shell, verify
from repo_agent_harness.models import CheckResult, HealthCheckConfig, HealthConfig, HealthSnapshot

CACHE_TTL_SECONDS = 300
_MAX_OUTPUT = 4000


class DiagnosticsGateway(Protocol):
    """Anything that can synchronously forward a Serena tool call from a worker thread."""

    def call_from_thread(self, name: str, arguments: dict) -> object:
        """Forward one tool call and return the raw CallToolResult."""


# ----------------------------------------------------------------------------- config


def load_config(root: str) -> HealthConfig:
    """Load health.yml from config resolution chain, falling back to built-in defaults."""
    from repo_agent_harness.paths import harness_home, repo_id  # noqa: PLC0415

    h = harness_home()
    rid = repo_id(root)
    candidates = [
        h / "repos" / rid / "health.yml",
        h / "harness-health.yml",
        Path(__file__).parent / "defaults" / "health.yml",
    ]
    if yaml is None:
        return HealthConfig()
    for path in candidates:
        if path.is_file():
            try:
                data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                return HealthConfig.model_validate(data)
            except (yaml.YAMLError, ValidationError) as exc:
                return HealthConfig(config_error=f"{path.name} invalid, using defaults: {exc}")
    return HealthConfig()


# ----------------------------------------------------------------------------- runners


def _from_verify(root: str, cfg: HealthCheckConfig) -> CheckResult:
    """Adapt a verify.run_kind result (lint/typecheck/test) into a CheckResult."""
    data = verify.run_kind(root, cfg.kind)
    skipped = bool(data.get("skipped"))
    return CheckResult(
        id=cfg.id,
        kind=cfg.kind,
        ok=None if skipped else bool(data.get("ok")),
        skipped=skipped,
        summary=f"via {data.get('via')}",
        output=shell.truncate(str(data.get("output") or ""), _MAX_OUTPUT),
        command=data.get("command"),
    )


def _git_check(root: str, cfg: HealthCheckConfig) -> CheckResult:
    """Worktree state: branch, dirty flag, ahead/behind upstream, merge conflicts."""
    st = git.status(root)
    conflicts = git.conflicted_files(root)
    parts = [f"branch {st['branch']}", "dirty" if st["dirty"] else "clean"]
    ab = git.ahead_behind(root)
    if ab is not None:
        parts.append(f"ahead {ab[0]}, behind {ab[1]}")
    if conflicts:
        parts.append(f"{len(conflicts)} conflicted file(s)")
    return CheckResult(
        id=cfg.id,
        kind=cfg.kind,
        ok=not conflicts,
        summary="; ".join(parts),
        output="\n".join(conflicts),
    )


def _command_check(root: str, cfg: HealthCheckConfig) -> CheckResult:
    """Run a custom argv-list command, gated by the repo's shell policy."""
    if not cfg.command:
        return CheckResult(id=cfg.id, kind=cfg.kind, skipped=True, summary="no command configured")
    cmdline = " ".join(cfg.command)
    decision = policies.check_command(cmdline, root)
    if not decision.allowed or decision.requires_confirmation:
        return CheckResult(
            id=cfg.id,
            kind=cfg.kind,
            skipped=True,
            command=cmdline,
            summary=f"refused by shell policy: {decision.reason}",
        )
    res = shell.run(cfg.command, cwd=root, timeout=cfg.timeout, max_chars=_MAX_OUTPUT)
    return CheckResult(
        id=cfg.id,
        kind=cfg.kind,
        ok=res.ok,
        summary="passed" if res.ok else ("timed out" if res.timed_out else f"exit {res.code}"),
        output=(res.stdout or res.stderr).strip(),
        command=cmdline,
    )


def _ci_check(root: str, cfg: HealthCheckConfig) -> CheckResult:
    """Latest workflow-run status on the configured (or current) branch via gh. Network."""
    if not shell.which("gh"):
        return CheckResult(id=cfg.id, kind=cfg.kind, skipped=True, summary="gh not available")
    branch = cfg.branch or git.status(root)["branch"]
    cmd = ["gh", "run", "list", "--branch", branch, "--limit", "1", "--json", "status,conclusion,headSha"]
    res = shell.run(cmd, cwd=root, timeout=cfg.timeout, max_chars=_MAX_OUTPUT)
    if not res.ok:
        return CheckResult(
            id=cfg.id,
            kind=cfg.kind,
            skipped=True,
            command=" ".join(cmd),
            summary=f"gh failed: {(res.stderr or res.stdout).strip()[:200]}",
        )
    try:
        runs = json.loads(res.stdout or "[]")
    except ValueError:
        runs = []
    if not runs:
        return CheckResult(id=cfg.id, kind=cfg.kind, skipped=True, summary=f"no workflow runs on {branch}")
    latest = runs[0]
    if latest.get("status") != "completed":
        return CheckResult(
            id=cfg.id,
            kind=cfg.kind,
            ok=None,
            command=" ".join(cmd),
            summary=f"CI {latest.get('status')} on {branch} ({latest.get('headSha', '')[:7]})",
        )
    conclusion = latest.get("conclusion")
    return CheckResult(
        id=cfg.id,
        kind=cfg.kind,
        ok=conclusion == "success",
        command=" ".join(cmd),
        summary=f"CI {conclusion} on {branch} ({latest.get('headSha', '')[:7]})",
    )


_DIAG_MAX_FILES = 10


def _tally_diagnostics(node: object, counts: dict[str, int], bucket: str | None = None) -> None:
    """Walk Serena's grouped diagnostics (path -> severity -> symbol -> results) counting leaves."""
    if isinstance(node, dict):
        for key, value in node.items():
            lowered = str(key).lower()
            next_bucket = "errors" if "error" in lowered else ("warnings" if "warn" in lowered else bucket)
            _tally_diagnostics(value, counts, next_bucket)
    elif isinstance(node, list) and bucket is not None:
        counts[bucket] += len(node)


def _count_diagnostics(result: object) -> dict[str, int]:
    """Extract error/warning counts from a get_diagnostics_for_file CallToolResult, defensively."""
    data = getattr(result, "structuredContent", None)
    if not data:
        for block in getattr(result, "content", None) or []:
            text = getattr(block, "text", None)
            if text:
                try:
                    data = json.loads(text)
                except ValueError:
                    data = None
                break
    counts = {"errors": 0, "warnings": 0}
    if isinstance(data, dict):
        _tally_diagnostics(data, counts)
    return counts


def _diagnostics_check(root: str, cfg: HealthCheckConfig, gateway: DiagnosticsGateway | None) -> CheckResult:
    """LSP diagnostics for changed files via the Serena gateway; degrades to skip without it."""
    if gateway is None or not hasattr(gateway, "call_from_thread"):
        return CheckResult(id=cfg.id, kind=cfg.kind, skipped=True, summary="serena gateway unavailable")
    changed = [f for f in git.changed_files(root) if (Path(root) / f).is_file()][:_DIAG_MAX_FILES]
    if not changed:
        return CheckResult(id=cfg.id, kind=cfg.kind, skipped=True, summary="no changed files to diagnose")
    errors, warnings, details = 0, 0, []
    for f in changed:
        try:
            result = gateway.call_from_thread("get_diagnostics_for_file", {"relative_path": f})
        except Exception as exc:  # noqa: BLE001 - any gateway failure must degrade to a skip, never crash health
            return CheckResult(id=cfg.id, kind=cfg.kind, skipped=True, summary=f"serena gateway error: {exc}")
        counts = _count_diagnostics(result)
        errors += counts["errors"]
        warnings += counts["warnings"]
        if counts["errors"] or counts["warnings"]:
            details.append(f"{f}: {counts['errors']} error(s), {counts['warnings']} warning(s)")
    return CheckResult(
        id=cfg.id,
        kind=cfg.kind,
        ok=errors == 0,
        summary=f"{errors} error(s), {warnings} warning(s) across {len(changed)} changed file(s)",
        output="\n".join(details),
    )


_RUNNERS = {
    "lint": _from_verify,
    "typecheck": _from_verify,
    "test": _from_verify,
    "git": _git_check,
    "command": _command_check,
    "ci": _ci_check,
}


def _run_check(root: str, cfg: HealthCheckConfig, gateway: DiagnosticsGateway | None) -> CheckResult:
    """Dispatch one check to its runner and stamp its duration."""
    start = time.monotonic()
    result = _diagnostics_check(root, cfg, gateway) if cfg.kind == "diagnostics" else _RUNNERS[cfg.kind](root, cfg)
    return result.model_copy(update={"duration_ms": int((time.monotonic() - start) * 1000)})


# ------------------------------------------------------------------------------ cache


@dataclass
class _CacheEntry:
    snapshot: HealthSnapshot
    status_hash: str
    monotonic: float
    dirty: bool = False


_CACHE: dict[str, _CacheEntry] = {}


def _status_hash(root: str) -> str:
    res = shell.run(["git", "status", "--porcelain"], cwd=root, timeout=15)
    return hashlib.sha256(res.stdout.encode("utf-8")).hexdigest()


def invalidate(root: str, paths: set[str] | None = None) -> None:
    """Mark the cached snapshot for ``root`` stale (the file watcher's interface)."""
    _ = paths  # reserved: per-path invalidation granularity
    entry = _CACHE.get(root)
    if entry is not None:
        entry.dirty = True


def cached(root: str) -> HealthSnapshot | None:
    """Return the cached snapshot with an honest stale flag, or None. Never runs checks."""
    entry = _CACHE.get(root)
    if entry is None:
        return None
    stale = entry.dirty or _status_hash(root) != entry.status_hash
    return entry.snapshot.model_copy(update={"provenance": "cache", "stale": stale})


# -------------------------------------------------------------------------------- run


def _fresh_cache_hit(root: str, refresh: bool, only: str | None) -> HealthSnapshot | None:
    """Return the cached snapshot when it is still trustworthy, else None."""
    if refresh or only is not None:
        return None
    entry = _CACHE.get(root)
    if entry is None or entry.dirty or time.monotonic() - entry.monotonic >= CACHE_TTL_SECONDS:
        return None
    if _status_hash(root) != entry.status_hash:
        return None
    return entry.snapshot.model_copy(update={"provenance": "cache", "stale": False})


def run(
    root: str,
    *,
    only: str | None = None,
    refresh: bool = False,
    gateway: DiagnosticsGateway | None = None,
) -> HealthSnapshot:
    """Produce a repository health snapshot from the repo's declarative checks.

    Args:
        root: Repository root directory.
        only: Run a single check by id (bypasses the cache, result not cached).
        refresh: Force a re-run even when a fresh cached snapshot exists.
        gateway: Serena gateway used by the diagnostics check (None degrades to skip).

    Returns:
        A HealthSnapshot; ok is False only when an executed check failed.
    """
    hit = _fresh_cache_hit(root, refresh, only)
    if hit is not None:
        return hit
    status_hash = _status_hash(root)
    cfg = load_config(root)
    selected = [c for c in cfg.checks if c.enabled and (only is None or c.id == only)]
    if only is not None and not selected:
        known = ", ".join(c.id for c in cfg.checks)
        results = [CheckResult(id=only, kind="unknown", skipped=True, summary=f"no such check id; known: {known}")]
    else:
        results = [_run_check(root, c, gateway) for c in selected]
    snapshot = HealthSnapshot(
        ok=not any(r.ok is False for r in results),
        checks=results,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        git_head=git.head(root),
        provenance="fresh",
        config_error=cfg.config_error,
    )
    if only is None:
        _CACHE[root] = _CacheEntry(snapshot=snapshot, status_hash=status_hash, monotonic=time.monotonic())
    return snapshot
