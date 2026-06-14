# Agent guide — raisl

<!-- repo-agent-harness:section:begin -->
## Working in this repo (repo-agent-harness)

This repository carries the **repo-agent-harness**: safe, deterministic, repo-aware
tooling for any coding agent. One MCP server (wired in `.mcp.json`) provides everything —
including Serena's semantic code tools, proxied as `serena_*` and launched on first use.
Agents without an MCP client use the same operations as CLI tools under `agent/tools/`.

### First, in a new session
- Call **`serena_initial_instructions`** before symbol work (Serena's usage manual is not
  injected automatically through the proxy), and **`serena_onboarding`** once per repo —
  it sets up Serena's project memories so symbol lookups work.

### Navigation & reading
- Use **Serena** first for symbols: `serena_find_symbol`, `serena_find_referencing_symbols`,
  `serena_get_symbols_overview`.
- Use the **repo-agent-harness** tools for repo facts: `repo_context_overview`,
  `repo_context_status`, `repo_context_relevant_files`, `repo_search_text`, `repo_search_files`.
- **Read precise ranges** with `repo_read_range`. Never recursively read the repo or dump whole files.

### Repo health
- `repo_health` reports what "healthy" means for this repo — lint/typecheck/tests for changed
  files, worktree state, LSP diagnostics, optional CI status. Configure it in `agent/health.yml`
  (add custom `command` checks; enable the `ci` check if network use is acceptable).

### Before editing
- Identify the relevant files (`repo_context_relevant_files` + Serena).
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
