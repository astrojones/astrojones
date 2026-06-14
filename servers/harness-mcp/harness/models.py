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


CheckKind = Literal["lint", "typecheck", "test", "git", "diagnostics", "ci", "command"]


class HealthCheckConfig(BaseModel):
    """One declarative health check from agent/health.yml."""

    id: str
    kind: CheckKind
    enabled: bool = True
    command: list[str] | None = Field(None, description="argv list for kind=command (never a shell string)")
    timeout: int = Field(120, ge=1, le=600)
    branch: str | None = Field(None, description="branch for kind=ci; defaults to the current branch")


def _default_checks() -> list[HealthCheckConfig]:
    return [
        HealthCheckConfig(id="lint", kind="lint"),
        HealthCheckConfig(id="typecheck", kind="typecheck"),
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


class HealthSnapshot(BaseModel):
    """A repository health snapshot with freshness provenance."""

    ok: bool
    checks: list[CheckResult]
    generated_at: str = Field(..., description="ISO-8601 UTC timestamp of the run")
    git_head: str = Field("", description="short HEAD sha at run time")
    provenance: Literal["fresh", "cache"] = "fresh"
    stale: bool = Field(False, description="True when the worktree changed since this snapshot was generated")
    config_error: str | None = None
