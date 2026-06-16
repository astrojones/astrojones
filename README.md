# astrojones

**The repo agent harness as a Claude Code plugin.** Install it once and every git repo you
open gets safe, deterministic, repo-aware tooling and symbol-level code navigation — instead
of a coding agent improvising with raw shell, and with **no harness files written into the repo**.

`astrojones` is client-agnostic in spirit: the same harness server runs for Claude Code (bundled
here, auto-connecting) and — pinned into a repo's `.mcp.json` — for CI and other MCP clients.
It is **generic**: nothing in it is tied to any one org. (The astrojones-specific deploy
layer lives in the separate, private [`deploy`](https://github.com/astrojones/deploy) plugin,
which builds on `astrojones`.)

## Install

```bash
/plugin marketplace add astrojones/claude-plugins
/plugin install astrojones@astrojones
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
- **Zero-footprint by default** — opening a repo writes no harness files into it (serena keeps
  its symbol index under `.serena/`, which it gitignores). The bundled server
  (tools, an always-read `instructions` guide, and the workflow prompts) is everything Claude
  Code needs. Materialize the on-disk harness (`agent/` policies + tools, an `AGENTS.md` guide)
  only on demand with `repo_bootstrap` / `/harness-init` — for per-repo customization or
  non-MCP clients (opencode/CI).
- **Safety hooks** — a safe-shell + secret-read guard (PreToolUse) and a post-edit
  verification nudge (PostToolUse).
- **Generic coding-workflow skills** — `bugfix`, `feature`, `refactor`, `test`, `implement`,
  `commit` (served as MCP prompts; see the drift note below), plus the Claude-Code-only `plan`
  skill: harness-native plan mode that drives the read-only `explorer` and `architect` subagents
  in place of the built-in `Explore`/`Plan` agents.
- **Workflow subagents** — `implementer`, `reviewer`, `test-runner` (TDD streams, diff
  review, narrow verification), `explorer` (read-only symbol navigation — the harness-native
  replacement for the built-in `Explore` agent; prefer it for any code exploration), and
  `architect` (read-only design/planning — the harness-native replacement for the built-in
  `Plan` agent; hands the build to `implementer`).

## Materializing the on-disk harness (opt-in)

By default the harness writes **nothing** into your repos — the bundled server delivers the
tools, the `instructions` guide, and the workflow prompts directly. Materialize the on-disk
harness only when you want editable per-repo config or need to support a non-MCP client:

| Path | When | Mechanism |
|------|------|-----------|
| **In-session, explicit** | On demand — to edit `agent/` policies/health, or to harness a freshly-created repo | Call the **`repo_bootstrap`** MCP tool (optionally `path=...` to target another repo, `pin=...` for a project `.mcp.json`). |
| **Fallback / CI** | Non-Claude-Code clients (opencode), CI, or the MCP server is unreachable | `/harness-init` runs the bundled CLI: `uv run --project "${CLAUDE_PLUGIN_ROOT}/servers/harness-mcp" repo-agent-harness bootstrap --target both`. |

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
claude --plugin-dir /path/to/astrojones    # test before publishing
```

The plugin index lives in [`astrojones/claude-plugins`](https://github.com/astrojones/claude-plugins);
this repo is just the plugin.
