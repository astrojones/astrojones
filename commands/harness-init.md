---
description: Scaffold the repo-agent-harness (agent/ policies + tools, AGENTS.md, .mcp.json, .opencode/opencode.json) into the current repository.
argument-hint: "[--force] [--agents-md auto|skip|overwrite] [--pin <sha>] [--target claude|opencode|both]"
allowed-tools: Bash, Read, Edit
---

Set up the **repo-agent-harness** in the current repository so a coding agent has safe,
deterministic, repo-aware tooling. The heavy lifting is done by the harness's own CLI — do
not copy files by hand.

> **Deprecation note:** the harness is now bootstrapped automatically by the plugin's
> load-time hook. This command remains as an explicit re-bootstrap path (force-overwrite,
> change the target surface, or refresh the opencode side) and as a fallback for sessions
> where the plugin's hook didn't run. For first-time setup in a normal session, you do
> NOT need to run this — the plugin does it.

1. Confirm the working directory is inside a git repo: `git rev-parse --show-toplevel`.
2. Run the deterministic installer. Pass through any flags the user gave (`--force`,
   `--agents-md auto|skip|overwrite`, `--pin <sha>`, `--target claude|opencode|both`):

   ```bash
   uvx --from "git+https://github.com/astrojones/repo-agent-harness@${HARNESS_SHA:-main}#subdirectory=mcp" \
     repo-agent-harness bootstrap --target both --json
   ```

   `bootstrap --target both` installs `agent/` (policies, manifest, tools), refreshes
   `AGENTS.md` (marker-delimited section, idempotent), and writes `.opencode/opencode.json`
   for opencode clients. Existing files are never overwritten without `--force`. Report
   `created`/`merged`/`skipped` to the user.

   **Note:** the MCP server is bundled in the astrojones-dev plugin and auto-connects —
   no `.mcp.json` is written by default. If the user needs a project-pinned entry for CI
   or non-Claude-Code clients, re-run with `--pin "${HARNESS_SHA}"`.

3. Tailor `agent/manifest.yml` to this repo (name, frameworks, important paths,
   entrypoints) and review `agent/policies/` for project-specific allow/deny rules.
4. Tell the user the harness is ready. Run `agent/tools/repo-overview` to confirm it
   is working (requires an active session with the plugin loaded).

Keep edits minimal; the defaults are intentionally conservative.
