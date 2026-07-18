# Agent guide — astrojones

<!-- repo-agent-harness:section:begin -->
## Working in this repo (repo-agent-harness)

This repository carries the **repo-agent-harness**: safe, deterministic, repo-aware
tooling for any coding agent. One MCP server (wired in `.mcp.json`) provides everything —
including Serena's semantic code tools, proxied as `serena_*` and launched on first use.
Agents without an MCP client use the same operations as CLI tools under `agent/tools/`.

### First, in a new session
- **Onboarding is automatic.** The harness seeds Serena's gate-flag at MCP connect, before
  you act — so symbol navigation is ready immediately. Just start navigating by symbol; no
  manual bootstrap call is required.
- **Durable project memory** is populated once per repo via a confirmed cognee ingest — run
  **`/astrojones:onboard`** to curate and load it (it cost-gates and asks before spending).
- `serena_initial_instructions` remains available if you want Serena's usage manual, but it
  is no longer a mandatory first step.

### Navigation & reading
- Use **Serena** first for symbols: `serena_find_symbol`, `serena_find_referencing_symbols`,
  `serena_get_symbols_overview`.
- Use the **repo-agent-harness** tools for repo facts: `repo_context_overview`,
  `repo_context_status`, `repo_context_relevant_files`, `repo_search_text`, `repo_search_files`.
- **Read precise ranges** with `repo_read_range`. Never recursively read the repo or dump whole files.
- **In Claude Code:** to map an unfamiliar or multi-file region, dispatch the **`explorer`** subagent — it runs this same Serena+harness navigation read-only and hands back a cited reading list instead of flooding the session. It is the harness-native replacement for the generic built-in `Explore` agent; prefer it for any code exploration.

### Repo health
- `repo_health` reports what "healthy" means for this repo — lint/typecheck/tests for changed
  files, worktree state, LSP diagnostics, optional CI status. Configure it in `agent/health.yml`
  (add custom `command` checks; enable the `ci` check if network use is acceptable).

### Before editing
- Identify the relevant files (`repo_context_relevant_files` + Serena; in Claude Code, the `explorer` subagent does this read-only and returns a reading list).
- For cross-file changes, run `repo_impact_file` and note the risk level. If risk is "high"
  (auth/payments/migrations/security/schema), confirm the plan first.

### After editing
- Run `agent/tools/safe-diff` and `repo_verify_changed` — lint/typecheck/test for the changed files only.

### Shell discipline
- Prefer the harness tools and `agent/tools/*` over raw shell.
- Destructive commands, secret-file reads, and `curl … | sh` are **blocked** by policy
  (`agent/policies/shell.yml`).
- `git push`, `git reset --hard`, and database migrations **require confirmation**.

### Local tools (`agent/tools/`, all support `--json`)
`repo-overview` · `safe-diff` · `impact <path>` · `lint-changed` · `typecheck-changed` · `test-changed` · `health`

Tune `agent/policies/*.yml`, `agent/health.yml`, and `agent/manifest.yml` for this repo.
<!-- repo-agent-harness:section:end -->

<!-- astrojones:onboard:begin -->
astrojones is the **repo-agent-harness** packaged as a Claude Code plugin: one bundled,
auto-connecting MCP server exposing deterministic `repo_*` tools and proxied `serena_*`
code navigation, plus safety hooks and generic coding-workflow skills and subagents. It
harnesses any repo automatically on connect (`AGENTS.md` is the opt-out marker).

## Tech stack
- **Languages:** Python (the harness MCP server + CLI, `servers/harness-mcp/repo_agent_harness/`, Python 3.13–3.14), TypeScript (the `opencode/` plugin half), Shell (`docker/`, `hooks/`, `run-mcp.sh`).
- **Python deps:** `fastmcp` + `mcp` (MCP server), `serena-agent` (pinned git sha — semantic code navigation, proxied as `serena_*`), `httpx` (async `cognee_client`), `tree-sitter` + `tree-sitter-language-pack` (static symbol index; pinned `<0.26` — 0.26 ABI-breaks the wheels), `psutil` (stale-child reaping in `gateway`), `pydantic` + `sqlmodel` (models, `sync_ledger`), `pyyaml`, `watchfiles` (`watcher`), `claude-agent-sdk`.
- **External services:** **cognee** (remote durable-memory graph, reached via `cognee_client`; `cognee_local` runs it in Docker with a **pgvector** sidecar), **Serena** (LSP-backed navigation server, launched on first use by `gateway`/`serena_daemon`), **claude-mem** (legacy local SQLite store the harness mirrors from, read-only, via `claude_mem_reader` + `cognee_sync`).
- **Tooling:** `uv` (env/runner), `ruff` (lint+format, preview rules), `ty` (type checker), `pytest` (+ pytest-cov, pytest-timeout); `vitest` + `tsc` for the opencode half.

## Commands (build/test/lint/run)
Python harness (run from repo root; the project lives in `servers/harness-mcp`):
- **Run MCP server:** `./run-mcp.sh` — or `uv run --project servers/harness-mcp repo-agent-harness-mcp`
- **CLI:** `uv run --project servers/harness-mcp repo-agent-harness <subcommand>` (e.g. `cognee-local up|status|down`, `deploy-status`)
- **Test:** `uv run --project servers/harness-mcp pytest`
- **Lint:** `uv run --project servers/harness-mcp ruff check` · **Format:** `ruff format`
- **Typecheck:** `uv run --project servers/harness-mcp ty check`
- **Dogfood the live tree:** `export REPO_AGENT_HARNESS_DEV_ROOT=/path/to/astrojones` so the server, hooks, and verification run your working-tree source instead of the frozen plugin-cache snapshot.

opencode half (`opencode/`): `npm test` (vitest) · `tsc` typecheck.

Prefer the harness tools over raw shell: `repo_verify_changed` runs lint/typecheck/test scoped to changed files; `repo_health` reports the repo's declarative health checks.

## Conventions
- **Commits:** Conventional Commits (`feat:`, `fix:`, `docs:`, `refactor:`, `chore:`). Stage files explicitly by path — never `git add -A`.
- **Python style (ruff preview):** google docstrings; **relative imports banned** (`flake8-tidy-imports`); line length 120; complexity capped (mccabe 10, max-args 5, max-statements 50); `Any` banned (`ANN401`); bandit (`S`) on. Per-file ignores calibrated for `tests/`, `deploy.py`, `cli.py`, etc.
- **Types:** `ty` with `unresolved-import`/`unresolved-reference`/`invalid-assignment` = error.
- **Memory-path isolation (I1/I2):** files on the memory path must not import `subprocess` or `claude_agent_sdk`; `claude_mem_reader` opens the claude-mem DB **read-only** (`mode=ro`).
- **Safety:** the **serena-gate** denies ungated native reads of code (navigate by symbol instead); **shell-policy** (`agent/policies/shell.yml`) blocks destructive commands, secret-file reads, and `curl … | sh`; `git push` / `git reset --hard` / migrations require confirmation. Resilience patterns: **circuit-breaker** in `cognee_client`/`cognee_sync`.
- **Durable memory:** curate before ingest; pin a fixed type **ontology** (`mem_ontology`) before loading; tag every item with a **node_set**; prove retrieval with canary `mem_search` queries.
- **TDD:** the `implement`/`feature`/`bugfix` workflows drive strict RED → GREEN → REFACTOR.

## Structure & entrypoints
- `servers/harness-mcp/repo_agent_harness/` — the harness package. Entrypoints: `server.py` (MCP server, `repo-agent-harness-mcp`), `cli.py` (`repo-agent-harness`), `agent_hooks.py` (`python -m repo_agent_harness.agent_hooks <event>`). Core modules: `gateway` (Serena child process), `serena_daemon`/`serena_gate`, `context`/`symbols` (repo facts + static index), `mem`/`cognee_client`/`cognee_local`/`cognee_sync`/`claude_mem_reader`/`sync_ledger` (durable memory + claude-mem mirror), `health`/`verify`/`detect`/`impact`/`deploy`/`drift`/`policies`/`scaffold`/`prompts_registry`.
- `agents/` — bundled subagents: `architect`, `explorer`, `implementer`, `reviewer`, `test-runner`.
- `skills/` — workflow skills: `bugfix`, `feature`, `refactor`, `test`, `implement`, `plan`, `commit`, `onboard`, `astrojones-cognee-doctor`, `astrojones-graph-tune`, `astrojones-mem-ingest-wisely`.
- `commands/` — `harness-init` (explicit fallback for non-MCP clients).
- `opencode/` — the TypeScript opencode plugin that materializes skills/commands/agents from the harness SSOT.
- `hooks/` — Claude Code hook shims (`pre_tool_use.py`, `_harness_shim.py`). `docker/` — e2e verification harness. `AGENTS.md` — the harness guide (harness section is machine-managed).
<!-- astrojones:onboard:end -->
