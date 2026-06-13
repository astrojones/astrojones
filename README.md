# raisl

**The repo agent harness as a Claude Code plugin.** Install it once and every git repo you
open is *born harnessed* — a coding agent gets safe, deterministic, repo-aware tooling and
symbol-level code navigation, instead of improvising with raw shell.

`raisl` is client-agnostic in spirit: the same harness server runs for Claude Code (bundled
here, auto-connecting) and — pinned into a repo's `.mcp.json` — for CI and other MCP clients.
It is **generic**: nothing in it is tied to any one org. (The astrojones-specific deploy
layer lives in the separate, private [`deploy`](https://github.com/astrojones/deploy) plugin,
which builds on `raisl`.)

## Install

```bash
/plugin marketplace add astrojones/claude-plugins
/plugin install raisl@astrojones
```

That's it. The harness MCP server is bundled and auto-connects at session start — no per-repo
setup.

## What you get

- **A bundled, auto-connecting MCP server** — deterministic `repo_*` tools (overview, status,
  search, precise range reads, impact, diff, change-verification, health, bootstrap) instead
  of raw shell.
- **Serena, proxied** through the same server as `serena_*` tools for semantic symbol
  navigation and editing — find definitions, references, call sites; edit by symbol, not by
  line guesswork.
- **Auto-bootstrap on connect** — opening a repo with `raisl` materializes the harness
  (`agent/` policies + tools, and the `AGENTS.md` harness section). **AGENTS.md is opt-out**:
  drop a `.harness-no-agents-md` sentinel at the repo root to keep `raisl` from writing it.
- **Safety hooks** — a safe-shell + secret-read guard (PreToolUse) and a post-edit
  verification nudge (PostToolUse).
- **Generic coding-workflow skills** — `bugfix`, `feature`, `refactor`, `test`, `implement`,
  `commit-semantic`.
- **Workflow subagents** — `implementer`, `reviewer`, `test-runner` (TDD streams, diff
  review, narrow verification), `explorer` (read-only symbol navigation), and
  `fullstack-architect` (typed UI⇄backend vertical slices).

## How the harness gets into a repo

| Path | When | Mechanism |
|------|------|-----------|
| **Auto** | You open an existing repo with `raisl` connected | The server bootstraps it on connect (idempotent; AGENTS.md opt-out). |
| **In-session, explicit** | A freshly-created repo, or on demand | Call the **`repo_bootstrap`** MCP tool (optionally `path=...` to target another repo, `pin=...` for a project `.mcp.json`). |
| **Fallback** | CI / non-Claude-Code clients, or the MCP server is unreachable | `/harness-init` runs the bundled CLI: `uv run --project "${CLAUDE_PLUGIN_ROOT}/servers/harness-mcp" repo-agent-harness bootstrap --target both`. |

The workflow skills (`bugfix`, `feature`, …) and `/harness-init` live in the harness server
as **prompts** (the single source of truth), exposed via `@mcp.prompt()` (Claude Code) and a
`repo_prompt_get(name)` tool (clients that only surface tools, e.g. opencode). The
`skills/<name>/SKILL.md` files are offline copies; `repo_drift_check` reports divergence as a
**warning, never an error**, and `repo_drift_sync` refreshes them.

## The discipline the harness expects

Orient with `repo_context_overview`; locate with Serena; read precise ranges (never dump whole
files); check blast radius with `repo_impact_file` before a cross-file edit; edit small; then
run `repo_verify_changed` (narrow lint/typecheck/tests for what changed, not the whole suite).
Raw shell is policy-bounded — destructive commands and secret reads are denied with an
actionable reason.

## The server

The MCP server is a vendored Python package at [`servers/harness-mcp/`](./servers/harness-mcp/)
(`repo-agent-harness`), launched by [`run-mcp.sh`](./run-mcp.sh) via
`uv run --project servers/harness-mcp`. It is tested in this repo:

```bash
uv run --project servers/harness-mcp pytest
```

## Local development

```bash
claude --plugin-dir /path/to/raisl    # test before publishing
```

The plugin index lives in [`astrojones/claude-plugins`](https://github.com/astrojones/claude-plugins);
this repo is just the plugin.
