"""Pydantic models: MCP tool inputs and the repo-health subsystem."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RelevantFilesIn(BaseModel):
    """Input model for repo_context_relevant_files."""

    task: str = Field(..., description="Natural-language description of the task")
    max_files: int = Field(8, ge=1, le=50)


class SearchTextIn(BaseModel):
    """Input model for repo_search_text."""

    pattern: str = Field(..., description="Substring or ripgrep pattern")
    paths: list[str] | None = Field(None, description="Optional path scope")
    limit: int = Field(20, ge=1, le=200)


class SearchFilesIn(BaseModel):
    """Input model for repo_search_files."""

    pattern: str = Field(..., description="Glob, e.g. '*.py' or 'src/*'")
    limit: int = Field(20, ge=1, le=200)


class ReadRangeIn(BaseModel):
    """Input model for repo_read_range."""

    path: str = Field(..., description="Repo-relative file path")
    start_line: int = Field(1, ge=1)
    end_line: int = Field(200, ge=1)


class ImpactIn(BaseModel):
    """Input model for repo_impact_file."""

    path: str = Field(..., description="Repo-relative file path")


class VerifyIn(BaseModel):
    """Input model for repo_verify_changed."""

    mode: str = Field("auto", description="Verification mode")


class DiffIn(BaseModel):
    """Input model for repo_diff_current."""

    context_lines: int = Field(3, ge=0, le=10)


class CheckCommandIn(BaseModel):
    """Input model for repo_policy_check_command."""

    command: str = Field(..., description="The shell command to evaluate against policy")


class MemSearchIn(BaseModel):
    """Input model for mem_search."""

    query: str = Field(..., description="Natural-language query against the memory graph")
    search_type: Literal["GRAPH_COMPLETION", "CHUNKS", "TEMPORAL", "CODING_RULES"] = "GRAPH_COMPLETION"
    dataset: str | None = Field(None, description="Dataset name; None = the user's default scope")
    node_name: list[str] | None = Field(
        None, description="Restrict to these node_set tags (belongs_to_set filter); None = the whole dataset"
    )
    top_k: int = Field(10, ge=1, le=50)


class MemRememberIn(BaseModel):
    """Input model for mem_remember."""

    text: str = Field(..., description="The fact/observation to store durably")
    dataset: str | None = Field(
        None, description="Target dataset name; None resolves via the conventions table (mem.resolve_dataset)"
    )
    node_set: list[str] | None = Field(None, description="Category tags, e.g. ['project_docs']")
    metadata: dict | None = Field(None, description="Optional key/value context folded into the text")


class MemIngestIn(BaseModel):
    """Input model for mem_ingest."""

    items: list[str] = Field(..., description="Curated documents to ingest")
    dataset: str = Field(..., description="Target dataset name")
    node_set: list[str] | None = Field(None, description="Category tags applied to every item")
    ontology_key: str | None = Field(
        None,
        description="pinned ontology key from mem_ontology; extraction uses this OWL vocabulary",
    )
    dry_run: bool = Field(False, description="Only return the cost estimate; write nothing")
    confirm: bool = Field(False, description="Accept an over-limit estimated cost")


class MemStatsIn(BaseModel):
    """Input model for mem_stats."""

    dataset: str = Field(..., description="Dataset name to report on")


class MemOntologyIn(BaseModel):
    """Input model for mem_ontology."""

    individuals: dict[str, str] = Field(..., description="Mapping of individual name -> fixed type")


# ------------------------------------------------------------------- mem_* results

# What a decoded cognee JSON body may be (transport-level payloads stay loosely typed;
# everything the harness itself asserts about them is lifted into the typed fields below).
Json = dict | list | str | int | float | bool | None


class MemIngestEstimate(BaseModel):
    """Cost pre-flight for a bulk ingest."""

    items: int
    estimated_tokens: int
    estimated_chunks: int
    estimated_cost_usd: float


class MemError(BaseModel):
    """Failure result shared by every mem_* operation (the error is the contract)."""

    error: str
    hint: str | None = None
    status: int | None = None
    available: list[str] | None = Field(None, description="known dataset names (unknown-dataset errors)")
    estimate: MemIngestEstimate | None = Field(None, description="cost estimate (ingest refusals)")


class MemSearchResult(BaseModel):
    """mem_search / mem_rules success result."""

    results: Json
    search_type: str
    dataset: str | None = None


class MemRememberResult(BaseModel):
    """mem_remember success result: the fact is stored; extraction continues in background."""

    queued: bool = True
    dataset: str
    add_id: str | None = None


class MemIngestResult(BaseModel):
    """mem_ingest outcome: either a dry-run estimate or a completed ship."""

    dataset: str
    estimate: MemIngestEstimate
    dry_run: bool = False
    ingested: int = 0
    fresh_dataset: bool | None = None
    serial_first: bool | None = None


class MemStatsResult(BaseModel):
    """mem_stats: dataset existence + pipeline status; graph counts are upstream-unsupported."""

    dataset: str
    dataset_id: str | None = None
    status: Json = None
    node_counts_supported: bool = False
    hint: str = "cognee exposes no census endpoint; graph counts unavailable upstream"


class MemOntologyResult(BaseModel):
    """mem_ontology: the uploaded (or already-present) ontology and its paired prompt."""

    ontology_key: str
    uploaded: bool
    individuals: int
    types: list[str]
    prompt: str


class MemMigrateResult(BaseModel):
    """migrate-serena-memories outcome: which notes shipped where (originals stay put)."""

    migrated: int
    files: list[str]
    dataset: str
    node_set: list[str]
    dry_run: bool = False
    estimate: MemIngestEstimate | None = None


class ClaudeMemMigrateResult(BaseModel):
    """migrate-claude-mem outcome: the dry-run readiness report or per-project shipped totals.

    Selection counters (observations/summaries/sessions/per_type) describe everything the
    filters matched; estimated_docs/per_project/estimate describe only what is still pending
    after ledger dedup — so a fully-resumed run reports the selection with 0 pending.
    """

    dataset: str
    granularity: str
    db: str
    dry_run: bool = True
    observations: int = 0
    summaries: int = 0
    sessions: int = 0
    per_project: dict[str, int] = Field(default_factory=dict, description="pending/shipped docs per project")
    per_type: dict[str, int] = Field(default_factory=dict, description="selected source rows per type")
    estimated_docs: int = 0
    skipped_dedup: int = 0
    shipped: int = 0
    node_set: list[str] = Field(default_factory=list, description="base tags; a project: tag is added per batch")
    estimate: MemIngestEstimate | None = None


class MemDoctorResult(BaseModel):
    """mem_doctor verdict: checkable memory health + competing-capture sentinels."""

    configured: bool
    reachable: bool = False
    authenticated: bool = False
    datasets: list[str] | None = None
    hints: list[str] = Field(default_factory=list)


CheckKind = Literal["lint", "typecheck", "test", "git", "diagnostics", "ci", "command"]


class HealthCheckConfig(BaseModel):
    """One declarative health check from agent/health.yml.

    The ``auto``/``min_interval_s``/``adaptive_factor`` trio governs the perception
    daemon (perception.py), which auto-runs ``auto=True`` checks in the background as
    files change. The effective minimum gap between runs is
    ``max(min_interval_s, adaptive_factor * last_runtime_s)`` so a slow check (a big
    test suite) throttles itself proportionally to how long it actually takes, while a
    fast check (lint) stays responsive. These fields are inert for ``repo_health`` /
    ``repo_verify_changed``, which always run on demand.
    """

    id: str
    kind: CheckKind
    enabled: bool = True
    command: list[str] | None = Field(None, description="argv list for kind=command (never a shell string)")
    timeout: int = Field(120, ge=1, le=600)
    branch: str | None = Field(None, description="branch for kind=ci; defaults to the current branch")
    auto: bool = Field(False, description="auto-run this check in the background as files change (perception daemon)")
    min_interval_s: float = Field(0, ge=0, description="hard floor on seconds between background auto-runs")
    adaptive_factor: float = Field(
        0, ge=0, description="background min interval also >= adaptive_factor * the check's last runtime"
    )


def _default_checks() -> list[HealthCheckConfig]:
    # lint/typecheck are cheap and pure -> auto-run them in the background (perception);
    # tests can have side effects and cost, so they stay opt-in (auto=False) per repo.
    return [
        HealthCheckConfig(id="lint", kind="lint", auto=True, adaptive_factor=8),
        HealthCheckConfig(id="typecheck", kind="typecheck", auto=True, adaptive_factor=8),
        HealthCheckConfig(id="tests", kind="test"),
        HealthCheckConfig(id="worktree", kind="git"),
        HealthCheckConfig(id="diagnostics", kind="diagnostics"),
        HealthCheckConfig(id="ci", kind="ci", enabled=False),
    ]


class HealthConfig(BaseModel):
    """The repo's health-check configuration (agent/health.yml), with safe defaults."""

    version: int = 1
    checks: list[HealthCheckConfig] = Field(default_factory=_default_checks)
    config_error: str | None = Field(None, description="set when agent/health.yml failed to parse")


# --------------------------------------------------------------------------- perception


class CheckVerdict(BaseModel):
    """The latest background result of one auto-run check (perception daemon)."""

    id: str
    kind: CheckKind
    ok: bool | None = Field(None, description="True pass, False fail, None skipped/never-run")
    summary: str = ""
    command: str | None = None
    ran_at: float = Field(0, description="epoch seconds when this verdict was produced")
    runtime_ms: int = 0


class GitState(BaseModel):
    """A point-in-time view of the worktree's git state, for transition detection."""

    branch: str = ""
    head: str = Field("", description="short HEAD sha")
    dirty: bool = False
    conflicted: list[str] = Field(default_factory=list, description="files with merge conflicts")


class PerceptionSnapshot(BaseModel):
    """The harness's current perception of the repo, maintained by the perception daemon.

    Written atomically to ``repo_state_dir(root)/perception.json`` whenever the daemon
    refreshes it; read by the ``repo_state`` tool (pull) and the delivery hooks (push).
    """

    verdicts: list[CheckVerdict] = Field(default_factory=list)
    git: GitState = Field(default_factory=GitState)
    serena_child_pid: int | None = Field(None, description="live child Serena PID, if launched (topology signal)")
    generated_at: str = Field("", description="ISO-8601 UTC timestamp of this snapshot")


class CheckResult(BaseModel):
    """Outcome of one health check; ok=None means skipped/unavailable."""

    id: str
    kind: str
    ok: bool | None = None
    skipped: bool = False
    summary: str = ""
    output: str = ""
    command: str | None = None
    duration_ms: int = 0


class InFlightCall(BaseModel):
    """One harness tool call currently executing on the Serena gateway."""

    tool: str
    cwd: str
    elapsed_s: float
    stalled: bool = False


class HookHeartbeat(BaseModel):
    """Last successful run of one Claude Code hook event (or async job like memify).

    ``last_success_at is None`` means "never ran" — surfaced explicitly because the hook
    shims fail open by contract, so a silently dead hook leaves no other trace. ``count``
    is best-effort/lossy (see paths.stamp_hook_heartbeat); only ts/existence is contractual.
    """

    event: str
    last_success_at: str | None = Field(None, description="ISO-8601 UTC timestamp; None = never ran")
    age_s: float | None = None
    count: int = 0


class HealthSnapshot(BaseModel):
    """A repository health snapshot with freshness provenance."""

    ok: bool
    checks: list[CheckResult]
    generated_at: str = Field(..., description="ISO-8601 UTC timestamp of the run")
    git_head: str = Field("", description="short HEAD sha at run time")
    provenance: Literal["fresh", "cache"] = "fresh"
    stale: bool = Field(False, description="True when the worktree changed since this snapshot was generated")
    config_error: str | None = None
    in_flight: list[InFlightCall] = Field(
        default_factory=list, description="harness tool calls executing on the Serena gateway at snapshot time"
    )
    hook_heartbeats: list[HookHeartbeat] = Field(
        default_factory=list, description="per-hook-event last-success stamps; last_success_at=None means never ran"
    )
