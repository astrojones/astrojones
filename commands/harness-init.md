---
description: Scaffold the repo-agent-harness (agent/ policies + tools, AGENTS.md, sha-pinned .mcp.json) into the current repository.
argument-hint: "[--force] [--agents-md auto|skip|overwrite]"
allowed-tools: Bash, Read, Edit
---

Set up the **repo-agent-harness** in the current repository so a coding agent has safe,
deterministic, repo-aware tooling. The heavy lifting is done by the harness's own CLI — do
not copy files by hand.

1. Confirm the working directory is inside a git repo: `git rev-parse --show-toplevel`.
2. Run the deterministic installer (same pinned command `/new-app` uses — keep the shas in
   sync; pass through any flags the user gave: `--force`, `--agents-md auto|skip|overwrite`):

   ```bash
   HARNESS_SHA="ebff259fb41b4db6fefdcfda549303d08e20868f"   # repo-agent-harness main sha; keep --from and --pin in sync
   uvx --from "git+https://github.com/astrojones/repo-agent-harness@${HARNESS_SHA}#subdirectory=mcp" \
     repo-agent-harness init --pin "${HARNESS_SHA}" --json
   ```

   It installs `agent/` (policies, manifest, tools), creates `.mcp.json` or merges the
   sha-pinned `repo-agent-harness` server entry into an existing one (removing any legacy
   standalone `serena` entry — Serena is proxied through the harness now), and creates
   `AGENTS.md` or appends/refreshes the marker-delimited harness section. Existing files
   are never overwritten without `--force`. Report `created`/`merged`/`skipped` to the user.
3. Tailor `agent/manifest.yml` to this repo (name, frameworks, important paths,
   entrypoints) and review `agent/policies/` for project-specific allow/deny rules.
4. Tell the user to restart their agent session so the `.mcp.json` server loads, then run
   `agent/tools/repo-overview` to confirm the harness is working.

Keep edits minimal; the defaults are intentionally conservative.
