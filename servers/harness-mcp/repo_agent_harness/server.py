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
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, override

import anyio
from pydantic import AliasChoices, Field

try:
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware, MiddlewareContext
except ImportError as exc:  # pragma: no cover
    msg = "the 'fastmcp' package is required: uv add fastmcp"
    raise SystemExit(msg) from exc

from repo_agent_harness import (
    context,
    deploy,
    drift,
    git,
    health,
    impact,
    perception,
    policies,
    prompts_registry,
    scaffold,
    symbols,
    verify,
    watcher,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.middleware import CallNext
    from fastmcp.tools import ToolResult
    from mcp import types as mt

LOG = logging.getLogger(__name__)


async def _cancel(task: asyncio.Task | None) -> None:
    """Cancel a lifespan background task and await its exit (no-op on None)."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncIterator[dict]:
    """Run the worktree watcher and the perception daemon for the server's lifetime.

    The watcher marks the health cache stale on worktree changes; checks only ever run on
    the next repo_health read. The perception daemon auto-runs cheap checks on change and
    maintains the snapshot read by repo_state + hooks. Connecting writes nothing into the
    repo — the ``agent/`` tree and the ``AGENTS.md`` guide are materialized only on demand
    via the ``repo_bootstrap`` tool / CLI.
    """
    _ = app
    root = git.repo_root()
    if root:
        with suppress(Exception):  # best-effort prewarm: tree-sitter/mtime-diff, no network, idempotent
            symbols.refresh(root)  # build the static symbol index now so the first overview is instant
    # a plain task, not a task group: the lifespan generator's yield must not sit
    # inside a cancel scope, or cancelled shutdown exits scopes in the wrong task
    # One watcher, two sinks: keep the lazy health cache honest AND feed the perception daemon,
    # which auto-runs cheap checks on change and maintains the snapshot read by repo_state + hooks.
    perception_daemon = perception.Perception(root) if root else None

    def _on_repo_change(changed: set[str]) -> None:
        if root is not None:
            health.invalidate(root, changed)
        if perception_daemon is not None:
            perception_daemon.note_change(changed)

    repo_watcher = watcher.RepoWatcher(root, _on_repo_change) if root else None
    watch_task = asyncio.create_task(repo_watcher.run()) if repo_watcher else None
    perception_task = asyncio.create_task(perception_daemon.run()) if perception_daemon else None
    try:
        yield {}
    finally:
        if perception_daemon is not None:
            perception_daemon.stop()
        await _cancel(perception_task)
        if repo_watcher is not None:
            repo_watcher.stop()
        await _cancel(watch_task)


_INSTRUCTIONS = """\
Safe, repo-aware tools for the git repo at the current working directory: repo facts
(repo_* tools) and static symbol navigation (repo_symbols_overview).

- Orient in code via repo_symbols_overview (static tree-sitter index — instant, no LSP);
  use repo_search_*/repo_read_range for files. Read precise ranges; never dump whole
  files or recursively read the tree. Native Read/Grep are a fallback ONLY when neither
  can answer — never for code discovery.
- Call repo_context_overview to orient (languages, entrypoints, important paths).
- In Claude Code, to map an unfamiliar or multi-file region dispatch the `explorer`
  subagent — it runs this same harness navigation read-only and returns a cited
  reading list. It is the harness-native replacement for the built-in `Explore` agent;
  prefer it for any code exploration.
- Workflow playbooks (bugfix, feature, refactor, test, implement, commit) are served as
  MCP prompts and via repo_prompt_get(name); the implement pipeline coordinates the
  implementer/reviewer/test-runner subagents.
- Run repo_verify_changed on the files you changed before declaring work done.
- Shell is policy-bounded: destructive commands and secret-file reads are blocked; git
  push and database migrations need confirmation. Check repo_policy_check_command if unsure.
- Connecting writes no harness files into the repo. Materialization is opt-in: call
  repo_bootstrap to write agent/ (editable policies + health) and an AGENTS.md guide — for
  per-repo customization or non-MCP clients (opencode/CI). If repo_context_overview's
  `harness` block already reports harnessed=true with a guide, read it for repo-specific
  overrides.
"""


_TOOL_TIMEOUT_S = 300


class ToolTimeoutMiddleware(Middleware):
    """Bound every tool dispatch with a timeout as the generic backstop.

    The default timeout sits clear above any per-tool deadline, so it stays a pure outer
    backstop: a runaway ``@mcp.tool`` handler (file/grep/shell) can never starve the host
    heartbeat.
    """

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Bound the dispatch and log a terminal line either way."""
        name = context.message.name
        timeout = _TOOL_TIMEOUT_S
        try:
            with anyio.fail_after(timeout):
                result = await call_next(context)
        except TimeoutError:
            LOG.warning("tool %s timed out after %.1fs", name, timeout)
            msg = f"tool {name} timed out after {timeout}s"
            raise TimeoutError(msg) from None
        LOG.debug("tool %s completed", name)
        return result


mcp = FastMCP("repo-agent-harness", instructions=_INSTRUCTIONS, lifespan=_lifespan)
mcp.add_middleware(ToolTimeoutMiddleware())

# Per-repo workflow prompts (SSOT). Claude Code clients see them via
# ``prompts/list``; opencode clients (and any other MCP client that does not
# surface raw prompts) read them through the ``repo_prompt_get`` tool wrapper
# below. The bodies live in ``prompts/<name>.md`` and are loaded once at
# import time by ``prompts_registry``.
prompts_registry.register(mcp)


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
    """Heuristically rank files relevant to a task (path/term matching)."""
    root = git.repo_root()
    return context.relevant_files(root, task, max_files) if root else _no_repo()


@mcp.tool()
def repo_search_text(
    pattern: Annotated[
        str, Field(description="Substring or ripgrep pattern", validation_alias=AliasChoices("pattern", "query"))
    ],
    paths: Annotated[
        list[str] | None, Field(description="Optional path scope", validation_alias=AliasChoices("paths", "path"))
    ] = None,
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> dict:
    """Search file contents (ripgrep). Secret-redacted, secret paths skipped, result-limited."""
    root = git.repo_root()
    return context.search_text(root, pattern, paths, limit) if root else _no_repo()


@mcp.tool()
def repo_search_files(
    pattern: Annotated[
        str, Field(description="Glob, e.g. '*.py' or 'src/*'", validation_alias=AliasChoices("pattern", "glob", "path"))
    ],
    limit: Annotated[int, Field(ge=1, le=200)] = 20,
) -> dict:
    """Find tracked files by glob (git ls-files); ignored files excluded."""
    root = git.repo_root()
    return context.search_files(root, pattern, limit) if root else _no_repo()


@mcp.tool()
def repo_read_range(
    path: Annotated[
        str, Field(description="Repo-relative file path", validation_alias=AliasChoices("path", "relative_path"))
    ],
    start_line: Annotated[int, Field(ge=1, validation_alias=AliasChoices("start_line", "start"))] = 1,
    end_line: Annotated[int, Field(ge=1, validation_alias=AliasChoices("end_line", "end"))] = 200,
) -> dict:
    """Read a bounded line range. Refuses secrets/binaries; blocks path traversal; line-capped."""
    root = git.repo_root()
    if not root:
        return _no_repo()
    return context.read_range(root, path, start_line, end_line)


@mcp.tool()
def repo_symbols_overview(
    path: Annotated[
        str | None, Field(description="Repo-relative file or directory prefix to scope to; None = whole repo")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=2000, description="Maximum symbols to return")] = 200,
) -> dict:
    """Symbol map from the static tree-sitter index — no LSP launch, always current.

    The preferred first move for code orientation: names, kinds, spans, and nesting per
    file (optionally scoped to a path prefix).
    """
    root = git.repo_root()
    inp = symbols.SymbolsOverviewIn(path=path, limit=limit)
    return symbols.overview(root, inp).model_dump(exclude_none=True) if root else _no_repo()


@mcp.tool()
def repo_impact_file(
    path: Annotated[str, Field(description="Repo-relative file path")],
) -> dict:
    """Heuristic blast radius: dependents, test targets, risk."""
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
    return health.run(root, only=check, refresh=refresh).model_dump() if root else _no_repo()


@mcp.tool()
def repo_state() -> dict:
    """Current harness *perception* of the repo: latest auto-run check verdicts + git state.

    The perception daemon runs cheap checks (lint/typecheck by default; tests opt-in) in the
    background as files change and keeps this snapshot fresh — so read it instead of calling
    repo_verify_changed / repo_health when you just want the latest known state cheaply; the
    checks have already run. Returns ``verdicts`` (id/kind/ok/summary/ran_at) and ``git``
    (branch/head/dirty/conflicted). Falls back to a git-only baseline
    in the brief window before the daemon's first run.
    """
    root = git.repo_root()
    return perception.current_state(root) if root else _no_repo()


@mcp.tool()
def repo_policy_check_command(
    command: Annotated[str, Field(description="Shell command to evaluate")],
) -> dict:
    """Check a shell command against the repo's policy BEFORE running it (deny/allow/confirm)."""
    root = git.repo_root()
    return policies.check_command(command, root).to_dict() if root else _no_repo()


@mcp.tool()
def repo_prompt_get(
    name: Annotated[str, Field(description="Prompt identifier (e.g. 'bugfix', 'feature', 'harness-init')")],
) -> dict:
    """Return a registered prompt body plus source-of-truth metadata.

    Most MCP clients surface prompts via ``prompts/list`` and ``prompts/get``;
    this tool is a JSON-returning wrapper for clients (notably opencode) that
    only surface MCP tools to the model. The body is the workflow itself —
    assistant-agnostic, no per-client framing.

    The ``source`` field is the path to the on-disk file relative to the
    ``repo_agent_harness`` package root; the ``checksum`` is the SHA-256 of
    the body. Drift-check tools compare the served body to the on-disk file
    using these two fields.
    """
    entry = prompts_registry.get(name)
    if entry is None:
        return {
            "ok": False,
            "name": name,
            "error": f"unknown prompt: {name!r}",
            "available": prompts_registry.list_names(),
        }
    return {
        "ok": True,
        "name": entry.name,
        "title": entry.title,
        "description": entry.description,
        "body": entry.body,
        "source": entry.source,
        "checksum": entry.checksum,
    }


@mcp.tool()
def repo_bootstrap_status() -> dict:
    """Read-only inspection of which per-repo harness files are present.

    Materialization is opt-in (the harness is zero-footprint on connect): it
    happens via the ``repo_bootstrap`` tool or the ``bootstrap``/``init`` CLI
    subcommands. This tool is the read-only counterpart — so the model can ask
    "is the harness materialized here?" without writing.

    Returns a dict with ``ok``, ``root``, and ``present`` (mapping of
    file to bool: ``mcp_json``, ``agent_tree``, ``agents_md``,
    ``opencode_json``).
    """
    root = git.repo_root()
    return scaffold.inspect_bootstrap(root) if root else _no_repo()


@mcp.tool()
def repo_bootstrap(
    path: Annotated[
        str | None,
        Field(
            description="Absolute path to the repo to harness; defaults to the server's current repo. "
            "/new-app passes the freshly-created app directory (the server's cwd is fixed at session start)."
        ),
    ] = None,
    target: Annotated[
        str,
        Field(
            description="Which surface to materialize: 'claude', 'opencode', 'antigravity', 'all', or 'both' (default)."
        ),
    ] = "both",
    agents_md: Annotated[
        str,
        Field(
            description="AGENTS.md handling: 'auto' (default — write/append the harness section), "
            "'overwrite', or 'skip'. When you materialize, the default writes the AGENTS.md guide."
        ),
    ] = "auto",
    pin: Annotated[
        str | None,
        Field(description="Commit SHA to pin the harness spec in .mcp.json (for CI / non-Claude-Code clients)."),
    ] = None,
) -> dict:
    """Materialize the per-repo harness in the current repo — the action behind ``repo_bootstrap_status``.

    Writes the ``agent/`` tree (always), the ``AGENTS.md`` harness section (``agents_md``,
    default ``auto``), the opencode surface (when ``target`` includes opencode), and — only
    with ``pin`` — a project-pinned ``.mcp.json`` for CI / non-Claude-Code clients.
    Idempotent: re-running against a bootstrapped repo is a no-op.

    Connecting is zero-footprint, so this is the opt-in lever that materializes harness
    files when you want them — to edit per-repo policies/health, to give a non-MCP client
    (opencode/CI) the ``agent/tools`` shims, or so ``/new-app`` / ``/harness-app`` can
    harness a freshly-created repo at scaffold time.

    Returns the standard bootstrap result dict (``ok``, ``root``, ``created``, ``merged``,
    ``replaced``, ``skipped``, ``removed``, ``next_steps``), or a no-repo error.
    """
    root = git.repo_root(path)
    return scaffold.bootstrap_repo(root, target=target, agents_md=agents_md, pin=pin) if root else _no_repo()


@mcp.tool()
def repo_drift_check() -> dict:
    """Compare the harness server's prompt bodies to the on-disk SKILL.md copies.

    Drift is a warning, never an error. The plugin's load-time hook calls
    this and emits a console.warn listing drifted files; the user is
    expected to refresh via ``sync_prompts`` (or the matching CLI
    subcommand ``repo-agent-harness sync-prompts``) when they want the
    offline copy updated.

    The comparison is body-only: YAML frontmatter differences and trailing
    whitespace are not flagged. The check walks the plugin repo's
    ``skills/<name>/SKILL.md`` directory; missing files are reported as
    "missing" (not drifted) so the operator can distinguish "needs first
    write" from "needs refresh".
    """
    root = git.repo_root()
    return drift.check_repo_drift(root) if root else _no_repo()


@mcp.tool()
def repo_drift_sync(
    force: Annotated[bool, Field(description="Overwrite even in-sync files")] = False,
) -> dict:
    """Refresh the plugin's SKILL.md copies to match the harness prompt bodies.

    Idempotent: by default only writes when the on-disk body has drifted
    or the file is missing. Pass ``force=True`` to overwrite every file,
    including in-sync ones (useful after a manual harness-side edit that
    you want reflected everywhere immediately).
    """
    root = git.repo_root()
    return drift.sync_prompts(root, force=force) if root else _no_repo()


@mcp.tool()
def repo_deploy_validate(
    root: Annotated[str | None, Field(description="Repo root (default: cwd)")] = None,
    repo: Annotated[str | None, Field(description="Repo name override (default: origin URL)")] = None,
    owner: Annotated[str | None, Field(description="GitHub org override (default: origin URL owner)")] = None,
) -> dict:
    """Run the org's hard deploy-rule checks against the current repo.

    Same logic as the plugin's ``agent/tools/deploy-validate`` shim, lifted
    into the harness server so any MCP client (Claude, opencode, vanilla
    Codex) can invoke it as a tool rather than spawning a subprocess.

    Returns the standard ``{ok, repo, root, findings}`` dict; ``ok`` is
    True iff no finding has ``level == "error"`` (warnings are allowed).
    """
    rootp = git.repo_root()
    if not rootp:
        return _no_repo()
    p = Path(rootp)
    return deploy.validate(p, deploy.repo_name(p, repo), deploy.origin_owner(p, owner))


@mcp.tool()
def repo_deploy_status(
    limit: Annotated[int, Field(ge=1, le=20, description="How many runs to fetch")] = 5,
) -> dict:
    """List the recent deploy workflow runs and the app's published URL.

    Thin wrapper over ``gh run list`` (no SSH). If ``gh`` is not installed
    or not authenticated, returns a structured ``{error, hint}`` instead
    of raising — the model can surface the hint to the user.
    """
    rootp = git.repo_root()
    if not rootp:
        return _no_repo()
    p = Path(rootp)
    return deploy.status(p, deploy.repo_name(p, None), limit, deploy.origin_owner(p, None))


@mcp.tool()
def repo_deploy_logs(
    run_id: Annotated[str, Field(description="GitHub Actions run id (from repo_deploy_status)")],
    tail: Annotated[int, Field(ge=1, le=5000, description="Number of log lines to return")] = 200,
) -> dict:
    """Fetch the failed-step logs of a deploy run.

    Thin wrapper over ``gh run view --log-failed``. Returns structured error
    on missing gh / unauthenticated / run-not-found — never raises.
    """
    rootp = git.repo_root()
    if not rootp:
        return _no_repo()
    p = Path(rootp)
    return deploy.logs(deploy.repo_name(p, None), run_id, tail, deploy.origin_owner(p, None))


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
