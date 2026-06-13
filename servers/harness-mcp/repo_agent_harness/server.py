"""FastMCP server exposing the repo-agent-harness core as MCP tools/resources.

Tool names use underscores (the Anthropic tool-name pattern ``^[a-zA-Z0-9_-]{1,64}$``
forbids dots); the dotted names in the docs map 1:1, e.g. ``repo.context.overview`` ->
``repo_context_overview``.

Resources (``repo://...``) expose the same computed data as MCP resources for clients
that surface them automatically. In Claude Code, resources are pull-only: agents must
call ``ListMcpResourcesTool``/``ReadMcpResourceTool`` explicitly — they are NOT ambient
context. The tools remain the primary interface for Claude Code agents.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

from pydantic import Field

try:
    from fastmcp import FastMCP
except ImportError as exc:  # pragma: no cover
    msg = "the 'fastmcp' package is required: uv add fastmcp"
    raise SystemExit(msg) from exc

from repo_agent_harness import context, gateway, git, health, impact, policies, verify, watcher

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

# Single owner of the child Serena process; created without connecting (lazy).
_serena = gateway.SerenaGateway(git.repo_root() or str(Path.cwd()))


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncIterator[dict]:
    """Run the repo watcher for the server's lifetime and reap Serena on shutdown.

    The watcher marks the health cache stale on worktree changes; checks only
    ever run on the next repo_health read. Serena is never started here — only
    the first serena_* tool call (or diagnostics health check) launches it.
    """
    _ = app
    root = git.repo_root()
    # a plain task, not a task group: the lifespan generator's yield must not sit
    # inside a cancel scope, or cancelled shutdown exits scopes in the wrong task
    repo_watcher = watcher.RepoWatcher(root, lambda paths: health.invalidate(root, paths)) if root else None
    watch_task = asyncio.create_task(repo_watcher.run()) if repo_watcher else None
    try:
        yield {}
    finally:
        if repo_watcher is not None:
            repo_watcher.stop()
        if watch_task is not None:
            watch_task.cancel()
            with suppress(asyncio.CancelledError):
                await watch_task
        with suppress(RuntimeError):  # defensive: the child dies with our stdio anyway
            await _serena.aclose()


_INSTRUCTIONS = """\
Safe, repo-aware tools for the git repo at the current working directory: repo facts
(repo_* tools) and semantic code navigation (serena_* tools, launched on first use —
call serena_initial_instructions before symbol work).

- Call repo_context_overview first. If its `harness` block reports harnessed=true, the
  repo carries an AGENTS.md "Working in this repo" section — read it for the full workflow.
- Prefer serena_* for symbols and repo_search_*/repo_read_range for files over reading
  whole files; read precise ranges, never dump whole trees.
- Run repo_verify_changed on the files you changed before declaring work done.
- Shell is policy-bounded: destructive commands and secret-file reads are blocked; git
  push and database migrations need confirmation. Check repo_policy_check_command if unsure.
"""

mcp = FastMCP("repo-agent-harness", instructions=_INSTRUCTIONS, lifespan=_lifespan)

for _proxied in gateway.proxied_tools(_serena):
    mcp.add_tool(_proxied)


def _no_repo() -> dict:
    return {"error": "not inside a git repository; start the server from a repo root"}


# --------------------------------------------------------------------------- tools


@mcp.tool()
def repo_context_overview() -> dict:
    """Summarize the repo: languages, package managers, entrypoints, important paths, available tools."""
    root = git.repo_root()
    return context.overview(root) if root else _no_repo()


@mcp.tool()
def repo_context_status() -> dict:
    """Git status: branch, dirty flag, changed/untracked files, last commit."""
    root = git.repo_root()
    return git.status(root) if root else _no_repo()


@mcp.tool()
def repo_context_relevant_files(
    task: Annotated[str, Field(description="Natural-language task description")],
    max_files: Annotated[int, Field(ge=1, le=50)] = 8,
) -> dict:
    """Heuristically rank files relevant to a task (path/term matching).

    This is NOT semantic search — for symbol-level relevance use Serena
    (find_symbol / find_referencing_symbols).
    """
    root = git.repo_root()
    return context.relevant_files(root, task, max_files) if root else _no_repo()


@mcp.tool()
def repo_search_text(
    pattern: Annotated[str, Field(description="Substring or ripgrep pattern")],
    paths: Annotated[list[str] | None, Field(description="Optional path scope")] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> dict:
    """Search file contents (ripgrep). Secret-redacted, secret paths skipped, result-limited."""
    root = git.repo_root()
    return context.search_text(root, pattern, paths, limit) if root else _no_repo()


@mcp.tool()
def repo_search_files(
    pattern: Annotated[str, Field(description="Glob, e.g. '*.py' or 'src/*'")],
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> dict:
    """Find tracked files by glob (git ls-files); ignored files excluded."""
    root = git.repo_root()
    return context.search_files(root, pattern, limit) if root else _no_repo()


@mcp.tool()
def repo_read_range(
    path: Annotated[str, Field(description="Repo-relative file path")],
    start_line: Annotated[int, Field(ge=1)] = 1,
    end_line: Annotated[int, Field(ge=1)] = 200,
) -> dict:
    """Read a bounded line range. Refuses secrets/binaries; blocks path traversal; line-capped."""
    root = git.repo_root()
    return context.read_range(root, path, start_line, end_line) if root else _no_repo()


@mcp.tool()
def repo_impact_file(
    path: Annotated[str, Field(description="Repo-relative file path")],
) -> dict:
    """Heuristic blast radius: dependents, test targets, risk. Confirm with Serena find_referencing_symbols."""
    root = git.repo_root()
    return impact.file_impact(root, path) if root else _no_repo()


@mcp.tool()
def repo_verify_changed(
    mode: Annotated[str, Field(description="Verification mode")] = "auto",
) -> dict:
    """Run narrow lint/typecheck/test for changed files only (never the full suite)."""
    root = git.repo_root()
    return verify.verify_changed(root, mode) if root else _no_repo()


@mcp.tool()
def repo_diff_current(
    context_lines: Annotated[int, Field(ge=0, le=10)] = 3,
) -> dict:
    """Current uncommitted diff (stat + unified), secret-redacted and truncated."""
    root = git.repo_root()
    return git.diff_current(root, context_lines) if root else _no_repo()


@mcp.tool()
def repo_health(
    check: Annotated[str | None, Field(description="Run only this check id from agent/health.yml")] = None,
    refresh: Annotated[bool, Field(description="Bypass the cache and re-run all checks")] = False,
) -> dict:
    """Repository health snapshot from the repo's declarative checks (agent/health.yml).

    Edit agent/health.yml to configure what "healthy" means for this repo
    (lint/typecheck/test, worktree state, custom commands, opt-in CI status).
    Snapshots carry provenance (fresh vs cache) and a stale flag.
    """
    root = git.repo_root()
    return health.run(root, only=check, refresh=refresh, gateway=_serena).model_dump() if root else _no_repo()


@mcp.tool()
def repo_policy_check_command(
    command: Annotated[str, Field(description="Shell command to evaluate")],
) -> dict:
    """Check a shell command against the repo's policy BEFORE running it (deny/allow/confirm)."""
    root = git.repo_root()
    return policies.check_command(command, root).to_dict() if root else _no_repo()


# ----------------------------------------------------------------------- resources


@mcp.resource("repo://overview")
def res_overview() -> str:
    """Expose the repo overview as a resource (pull-only in CC — use repo_context_overview tool for agents)."""
    root = git.repo_root()
    return json.dumps(context.overview(root) if root else _no_repo(), indent=2)


@mcp.resource("repo://policies/{name}")
def res_policy(name: str) -> str:
    """Return the named policy file content, resolved from the harness config chain."""
    root = git.repo_root()
    if not root:
        return "# not inside a git repository"
    from repo_agent_harness.policies import _find_config  # noqa: PLC0415

    p = _find_config(root, name)
    return p.read_text() if p is not None else f"# no {name}.yml configured; harness defaults apply"


@mcp.resource("repo://impact/{path}")
def res_impact(path: str) -> str:
    """Heuristic blast radius for a repo-relative file, as a resource."""
    root = git.repo_root()
    return json.dumps(impact.file_impact(root, path) if root else _no_repo(), indent=2)


@mcp.resource("repo://health")
def res_health() -> str:
    """Expose the cached health snapshot as a resource (never runs checks, pull-only in CC)."""
    root = git.repo_root()
    if not root:
        return json.dumps(_no_repo(), indent=2)
    snap = health.cached(root)
    if snap is None:
        return json.dumps({"info": "no health snapshot yet; call repo_health to generate one"}, indent=2)
    return json.dumps(snap.model_dump(), indent=2)


def main() -> None:
    """Console-script entry point: run the MCP server over stdio."""
    mcp.run()


if __name__ == "__main__":  # pragma: no cover
    main()
