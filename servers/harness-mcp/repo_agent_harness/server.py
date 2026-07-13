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
import yaml
from pydantic import Field

try:
    from fastmcp import FastMCP
    from fastmcp.server.middleware import Middleware, MiddlewareContext
except ImportError as exc:  # pragma: no cover
    msg = "the 'fastmcp' package is required: uv add fastmcp"
    raise SystemExit(msg) from exc

from repo_agent_harness import (
    capture,
    cognee_client,
    context,
    deploy,
    drift,
    gateway,
    git,
    health,
    impact,
    mem,
    models,
    perception,
    policies,
    prompts_registry,
    scaffold,
    serena_gate,
    verify,
    watcher,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from fastmcp.server.middleware import CallNext
    from fastmcp.tools import ToolResult
    from mcp import types as mt

LOG = logging.getLogger(__name__)

# Single owner of the child Serena process; created without connecting (lazy).
# The root is resolved on the first serena_* call — not here at import time — so the
# child Serena attaches to the real project even when the server process started
# elsewhere (e.g. $HOME in a cloud session, before CLAUDE_PROJECT_DIR/cwd is settled).
_serena = gateway.SerenaGateway(git.repo_root)


async def _cancel(task: asyncio.Task | None) -> None:
    """Cancel a lifespan background task and await its exit (no-op on None)."""
    if task is None:
        return
    task.cancel()
    with suppress(asyncio.CancelledError):
        await task


@asynccontextmanager
async def _lifespan(app: FastMCP) -> AsyncIterator[dict]:
    """Run the worktree watcher for the server's lifetime and reap Serena on shutdown.

    Connecting writes only a one-time Serena ``project_overview`` memory under ``.serena/``
    (gitignored, like Serena's own symbol index) so the repo is onboarded from session one
    without depending on the agent running the onboarding ceremony; no other repo files are
    touched. The ``agent/`` tree and the ``AGENTS.md`` guide are still materialized only on
    demand via the ``repo_bootstrap`` tool / CLI. The watcher marks the health cache stale on
    worktree changes; checks only ever run on the next repo_health read. Serena is never
    started here — only the first serena_* tool call (or diagnostics health check)
    launches it.
    """
    _ = app
    root = git.repo_root()
    with suppress(Exception):  # best-effort: never let process cleanup block startup
        gateway.reap_stale_serena_children(root)  # kill orphaned Serena children from prior versions
    _seed_serena_languages(root)  # ensure every repo language is active before Serena launches
    _autoseed_onboarding(root)  # one-time, best-effort: onboard the repo before the agent acts
    # a plain task, not a task group: the lifespan generator's yield must not sit
    # inside a cancel scope, or cancelled shutdown exits scopes in the wrong task
    # One watcher, two sinks: keep the lazy health cache honest AND feed the perception daemon,
    # which auto-runs cheap checks on change and maintains the snapshot read by repo_state + hooks.
    perception_daemon = perception.Perception(root, gateway=_serena) if root else None

    def _on_repo_change(changed: set[str]) -> None:
        if root is not None:
            health.invalidate(root, changed)
        if perception_daemon is not None:
            perception_daemon.note_change(changed)

    repo_watcher = watcher.RepoWatcher(root, _on_repo_change) if root else None
    watch_task = asyncio.create_task(repo_watcher.run()) if repo_watcher else None
    # Pre-warm the Serena session in the background so the first code-navigation call
    # does not pay the full cold-boot (spawn + LSP start) cost while the UI waits.
    warm_task = _serena.warm() if root else None
    perception_task = asyncio.create_task(perception_daemon.run()) if perception_daemon else None
    # Capture drain: ship hook-enqueued rows to cognee in-process. Only when configured —
    # without a remote there is nothing to drain into (and hooks don't enqueue either).
    cognee = cognee_client.get_client()
    brain = capture.BrainCapture(root, cognee) if root and cognee.configured else None
    brain_task = asyncio.create_task(brain.run()) if brain else None
    try:
        yield {}
    finally:
        await _cancel(warm_task)
        if perception_daemon is not None:
            perception_daemon.stop()
        await _cancel(perception_task)
        if repo_watcher is not None:
            repo_watcher.stop()
        await _cancel(watch_task)
        if brain is not None:
            brain.stop()
        await _cancel(brain_task)
        with suppress(RuntimeError):  # defensive: the child dies with our stdio anyway
            await _serena.aclose()


_INSTRUCTIONS = """\
Safe, repo-aware tools for the git repo at the current working directory: repo facts
(repo_* tools) and semantic code navigation (serena_* tools, launched on first use).

**For any task that touches code, your FIRST action is `serena_initial_instructions`** — it
loads the Serena navigation manual and launches the symbol server. Do this before any native
Read/Grep of code, then navigate by symbol. (Skip only for pure non-code chores.)
**If it reports the project is not onboarded, run `serena_onboarding` and complete it** (write
the project memories it asks for) — a one-time per-repo step — before deep work.

- Navigate by symbol: serena_* (find_symbol, find_referencing_symbols, get_symbols_overview)
  for code; repo_search_*/repo_read_range for files. Read precise ranges; never dump whole
  files or recursively read the tree. Native Read/Grep are a fallback ONLY when Serena cannot
  answer (non-code files, not-yet-indexed) — never for code discovery.
- Call repo_context_overview to orient (languages, entrypoints, important paths).
- In Claude Code, to map an unfamiliar or multi-file region dispatch the `explorer`
  subagent — it runs this same serena+harness navigation read-only and returns a cited
  reading list. It is the harness-native replacement for the built-in `Explore` agent;
  prefer it for any code exploration.
- Workflow playbooks (bugfix, feature, refactor, test, implement, commit) are served as
  MCP prompts and via repo_prompt_get(name); the implement pipeline coordinates the
  implementer/reviewer/test-runner subagents.
- Durable memory lives in the remote cognee graph via the mem_* tools: mem_search /
  mem_rules to recall, mem_remember for single facts, mem_ingest for curated bulk loads
  (cost-gated), mem_ontology to pin the type vocabulary, mem_doctor when memory misbehaves.
- Run repo_verify_changed on the files you changed before declaring work done.
- Shell is policy-bounded: destructive commands and secret-file reads are blocked; git
  push and database migrations need confirmation. Check repo_policy_check_command if unsure.
- Connecting writes no harness files into the repo (serena keeps its symbol index under
  `.serena/` on first navigation). Materialization is opt-in: call
  repo_bootstrap to write agent/ (editable policies + health) and an AGENTS.md guide — for
  per-repo customization or non-MCP clients (opencode/CI). If repo_context_overview's
  `harness` block already reports harnessed=true with a guide, read it for repo-specific
  overrides.
"""


class ToolTimeoutMiddleware(Middleware):
    """Bound every tool dispatch with :func:`gateway.tool_timeout` and track it in-flight.

    The Serena proxy path is already bounded inside :meth:`gateway.SerenaGateway.call`; this is
    the backstop for the generic ``@mcp.tool`` handlers (file/grep/shell) that otherwise have no
    deadline, so a runaway handler can never starve the host heartbeat. The default
    :func:`gateway.tool_timeout` sits clear above Serena's own dispatch reap, so for a serena_*
    call this deadline never fires first — it stays a pure outer backstop.

    Every dispatch is registered into the shared ``_serena`` in-flight registry (the single source
    of truth, diagnostics #26): generic tools never otherwise touch the gateway, so the middleware
    is where they become visible to :meth:`gateway.SerenaGateway.in_flight_snapshot`.
    """

    @override
    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        """Bound the dispatch, register it in-flight, and log a terminal line either way."""
        name = context.message.name
        timeout = gateway.tool_timeout()
        cwd = git.repo_root() or str(Path.cwd())
        with _serena.register_inflight(name, cwd):
            try:
                with anyio.fail_after(timeout):
                    result = await call_next(context)
            except TimeoutError:
                LOG.warning("tool %s timed out after %.1fs", name, timeout)
                raise gateway.ToolTimeoutError(tool=name, timeout_s=timeout) from None
            LOG.debug("tool %s completed", name)
            return result


mcp = FastMCP("repo-agent-harness", instructions=_INSTRUCTIONS, lifespan=_lifespan)
mcp.add_middleware(ToolTimeoutMiddleware())

for _proxied in gateway.proxied_tools(_serena):
    mcp.add_tool(_proxied)

# Per-repo workflow prompts (SSOT). Claude Code clients see them via
# ``prompts/list``; opencode clients (and any other MCP client that does not
# surface raw prompts) read them through the ``repo_prompt_get`` tool wrapper
# below. The bodies live in ``prompts/<name>.md`` and are loaded once at
# import time by ``prompts_registry``.
prompts_registry.register(mcp)


def _no_repo() -> dict:
    return {"error": "not inside a git repository; start the server from a repo root"}


def _serena_read_gate(root: str, path: str) -> dict | None:
    """Refuse a pre-onboarding whole-file *code* read so repo_read_range can't bypass onboarding.

    Returns an onboarding directive (as a normal tool-error dict) when the gate is active, the
    target is a code file, and the repo is not yet onboarded; otherwise None (proceed). Mirrors
    the native-Read gate in agent_hooks so the two stay in lockstep — closing the escape observed
    in session 9e6fd520, where the agent read code via repo_read_range and never onboarded.
    """
    if serena_gate.gate_disabled() or not serena_gate.is_code_file(path):
        return None
    try:
        if serena_gate.is_onboarded(Path(root).resolve()):
            return None
    except OSError:
        return None  # fail open: uncertainty must never block a read
    return {"error": serena_gate.UNBOARDED_MSG, "path": path}


def _seed_overview_md(ov: dict) -> str:
    """Render a portable project-overview memory from repo_context_overview output.

    Excludes machine-specific fields (absolute root, locally-installed tools) so the memory is
    safe to commit (`.serena/memories/`) and share across clones.
    """

    def _join(key: str) -> str:
        vals = ov.get(key) or []
        return ", ".join(str(v) for v in vals) if vals else "—"

    return (
        f"# Project overview: {ov.get('name', 'project')}\n\n"
        "_Auto-generated by the repo-agent-harness on first connect, so Serena is onboarded from "
        "session one. Enrich it with `serena_write_memory` as you learn the project._\n\n"
        f"- **Languages:** {_join('languages')}\n"
        f"- **Frameworks:** {_join('frameworks')}\n"
        f"- **Package managers:** {_join('package_managers')}\n"
        f"- **Entrypoints:** {_join('entrypoints')}\n"
        f"- **Top-level paths:** {_join('important_paths')}\n\n"
        "Navigate code by symbol (serena_get_symbols_overview / serena_find_symbol); read precise "
        "ranges with repo_read_range. Native whole-file Read of code is gated on purpose.\n"
    )


def _seed_serena_languages(root: str | None) -> None:
    """Ensure ``.serena/project.yml`` lists every language the repo contains, before Serena launches.

    Serena, started with only ``--project``, auto-detects a single dominant language and then
    *raises* on symbol extraction for files of any other language present in the repo (e.g.
    ``Cannot extract symbols ... Active languages: ['python']`` for a ``.ts`` file in a
    Python-dominant repo). A symbol-driven agent then has no anchor and silently writes nothing.
    Merging the full detected language list in makes the right language servers start.

    Runs at startup before the lazy Serena launch, so the harness config wins the race. Serena
    writes its own ``project.yml`` (dominant language only), so this *merges* missing languages
    into an existing list rather than only seeding fresh repos — that is what reaches the repos
    that already hit the bug. Idempotent (no write when nothing is missing) and best-effort
    (never raises into startup). The yaml round-trip drops Serena's template comments only on the
    one merge that actually adds a language; ``.serena/`` is gitignored and regenerable.
    """
    if root is None or serena_gate.gate_disabled():
        return
    with suppress(OSError, ValueError, yaml.YAMLError):  # best-effort: never raise into startup
        rootp = Path(root).resolve()
        wanted = context.serena_languages(root)
        if not wanted:
            return
        cfg = rootp / ".serena" / "project.yml"
        data: dict = {}
        if cfg.exists():
            loaded = yaml.safe_load(cfg.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        current = [str(x) for x in data["languages"]] if isinstance(data.get("languages"), list) else []
        merged = current + [k for k in wanted if k not in current]
        if merged == current:
            return
        data["languages"] = merged
        data.setdefault("project_name", rootp.name)
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _autoseed_onboarding(root: str | None) -> None:
    """Write an initial Serena project memory on first connect to a not-yet-onboarded repo.

    Onboarding cannot be forced on the agent — no Claude Code mechanism compels a tool call, and
    every "please onboard" directive is skippable (the agent will read code via any ungated path
    instead). So the harness performs the one-time step itself: a deterministic project_overview
    memory derived from repo_context_overview, written *before the agent acts*, independent of
    which read tool it reaches for. This flips the repo to "onboarded" so future sessions inherit
    persistent Serena memory and the gate settles into steady Serena-first mode.

    Idempotent and best-effort: skips when the gate is disabled, outside a git repo, or already
    onboarded; never raises into server startup.
    """
    if root is None or serena_gate.gate_disabled():
        return
    try:
        rootp = Path(root).resolve()
        if serena_gate.is_onboarded(rootp):
            return
        mem_dir = rootp / ".serena" / "memories"
        mem_dir.mkdir(parents=True, exist_ok=True)
        (mem_dir / "project_overview.md").write_text(_seed_overview_md(context.overview(root)), encoding="utf-8")
    except (OSError, ValueError):
        return  # best-effort: a seeding failure must never block server startup


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
    """Read a bounded line range. Refuses secrets/binaries; blocks path traversal; line-capped.

    Pre-onboarding it refuses *code* files (mirroring the native-Read gate) so onboarding cannot
    be skipped via this tool; once the repo is onboarded it is the blessed precise-range reader.
    """
    root = git.repo_root()
    if not root:
        return _no_repo()
    gated = _serena_read_gate(root, path)
    return gated if gated is not None else context.read_range(root, path, start_line, end_line)


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
def repo_state() -> dict:
    """Current harness *perception* of the repo: latest auto-run check verdicts + git state.

    The perception daemon runs cheap checks (lint/typecheck by default; tests opt-in) in the
    background as files change and keeps this snapshot fresh — so read it instead of calling
    repo_verify_changed / repo_health when you just want the latest known state cheaply; the
    checks have already run. Returns ``verdicts`` (id/kind/ok/summary/ran_at), ``git``
    (branch/head/dirty/conflicted), and ``serena_child_pid``. Falls back to a git-only baseline
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
        Field(description="Which surface to materialize: 'claude', 'opencode', or 'both' (default)."),
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


# ----------------------------------------------------------------- durable memory (cognee)


@mcp.tool()
async def mem_search(inp: models.MemSearchIn) -> dict:
    """Recall from the durable memory graph (remote cognee)."""
    return (await mem.search(inp)).model_dump(exclude_none=True)


@mcp.tool()
async def mem_rules(
    query: Annotated[str, Field(description="What rules to retrieve, e.g. 'python error handling'")],
    top_k: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """Retrieve distilled coding rules from memory (CODING_RULES search)."""
    return (await mem.rules(query, top_k)).model_dump(exclude_none=True)


@mcp.tool()
async def mem_remember(inp: models.MemRememberIn) -> dict:
    """Store one fact durably: fast /add, then background /cognify (never blocks on extraction)."""
    return (await mem.remember(inp)).model_dump(exclude_none=True)


@mcp.tool()
async def mem_ingest(inp: models.MemIngestIn) -> dict:
    """Bulk-ingest curated items — cost-gated (refuses expensive runs unless confirm=true)."""
    return (await mem.ingest(inp)).model_dump(exclude_none=True)


@mcp.tool()
async def mem_stats(inp: models.MemStatsIn) -> dict:
    """Best-effort dataset stats (existence, pipeline status); honest about unsupported counts."""
    return (await mem.stats(inp)).model_dump(exclude_none=True)


@mcp.tool()
async def mem_ontology(inp: models.MemOntologyIn) -> dict:
    """Generate + idempotently upload a NamedIndividual ontology; returns the paired extraction prompt."""
    return (await mem.ontology(inp)).model_dump(exclude_none=True)


@mcp.tool()
async def mem_doctor() -> dict:
    """Cognee memory health: configured/reachable/authenticated + competing-capture sentinels."""
    return (await mem.doctor()).model_dump(exclude_none=True)


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
