# /harness-init — one-time bootstrap of the per-repo harness

> **Deprecation note:** this workflow is now automatic. Loading the harness
> MCP server in a repo is sufficient — the `repo_bootstrap` tool (or the
> `init` CLI subcommand) materializes the missing pieces on first use. Keep
> this prompt as a fallback for environments where the MCP server is
> unreachable (e.g., CI without the plugin) and an explicit bootstrap is
> required.

When invoked, the workflow is:

1. **Check whether the harness is already present** — look for `agent/`,
   `AGENTS.md`, and `.mcp.json` in the repo root. If all three exist, report
   the existing state and ask whether the user wants a refresh or a force
   overwrite.
2. **Call `repo_bootstrap(target="both")`** if the MCP server is reachable.
   The tool returns the canonical bundle as a JSON document and is idempotent.
3. **Otherwise, run the CLI:**
   ```bash
   repo-agent-harness init --agents-md auto [--pin <sha>] [--force]
   ```
   `--pin` is only needed for non-plugin environments (the plugin auto-connects
   the harness server).
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
