# /harness-init — one-time bootstrap of the per-repo harness

> **Deprecation note:** this workflow is now automatic. A plugin's load-time
> hook runs the `bootstrap` CLI subcommand on first use, so the harness
> materializes itself. Use `repo_bootstrap_status` (read-only) to inspect
> what's present, and the `bootstrap` CLI to (re)materialize. Keep this prompt
> as a fallback for environments where the bootstrap hook does not run (e.g.,
> CI without the plugin) and an explicit bootstrap is required.

When invoked, the workflow is:

1. **Check whether the harness is already present** — look for `agent/`,
   `AGENTS.md`, and `.mcp.json` in the repo root. If all three exist, report
   the existing state and ask whether the user wants a refresh or a force
   overwrite.
2. **Inspect current state with `repo_bootstrap_status`** if the MCP server is
   reachable. It is read-only — it reports what's already present without
   writing anything (there is no `repo_bootstrap` write tool; materialization
   is the CLI's job).
3. **Materialize with the `bootstrap` CLI:**
   ```bash
   repo-agent-harness bootstrap --target both --agents-md auto [--pin <sha>] [--force]
   ```
   The subcommand emits the canonical bundle as JSON and is idempotent. `--pin`
   is only needed for non-plugin environments (the plugin auto-connects the
   harness server and runs `bootstrap` for you).
4. **Verify** the bootstrap:
   - `agent/manifest.yml` exists and lists the active policy files.
   - `agent/tools/` has the `repo-overview` / `safe-diff` / `test-changed` /
     `lint-changed` / `typecheck-changed` shims.
   - `AGENTS.md` has a `<!-- repo-agent-harness:section:begin/end -->` block
     (the auto-managed section, not a hand-edited copy).
   - `.mcp.json` has a `repo-agent-harness` server entry (only if `--pin` /
     `--spec` was used).
5. **Tell the user** what was created, what was merged, and what was skipped.
   Point at the first tool call they should make (`repo_context_overview`)
   to confirm the harness is live.
