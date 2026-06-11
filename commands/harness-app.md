---
description: Retrofit an existing astrojones app with the repo-agent-harness + deploy tools
argument-hint: <app-name> [--path <local-checkout>]
allowed-tools: Bash, Read, Write, Edit, Glob, Grep
---

Bring an existing `astrojones` app repo up to the scaffold standard: the repo carries its
own agent harness and deploy tools, so any MCP-capable coding assistant works in it
safely and knows the deploy pipeline. (`/new-app` does all of this automatically for new
apps; this command retrofits old ones.)

App is `$1`. Work in `--path` if given, else in `./$1` if it exists, else clone:
`gh repo clone astrojones/$1 && cd $1`.

Steps:

1. **Install the harness** (same pinned command `/new-app` uses — keep the shas in sync):
   ```bash
   HARNESS_SHA="d4e0f6597764251bdf4497b856c01c4cb5eac12e"   # agentic-repo main sha; keep --from and --pin in sync
   uvx --from "git+https://github.com/astrojones/agentic-repo@${HARNESS_SHA}#subdirectory=mcp" \
     repo-agent-harness init --pin "${HARNESS_SHA}" --json
   ```
   This installs `agent/` (policies + manifest + harness tools), creates or merges
   `.mcp.json`, and creates `AGENTS.md` or appends the harness section to an existing
   one. It never overwrites existing files — report `created`/`merged`/`skipped`.

2. **Copy the org deploy tools** (the harness is generic; these are astrojones-specific):
   ```bash
   cp "${CLAUDE_PLUGIN_ROOT}/template/_shared/agent/tools/deploy-validate" agent/tools/
   cp "${CLAUDE_PLUGIN_ROOT}/template/_shared/agent/tools/deploy-status" agent/tools/
   cp "${CLAUDE_PLUGIN_ROOT}/template/_shared/agent/tools/deploy-logs" agent/tools/
   chmod +x agent/tools/*
   ```

3. **Complete AGENTS.md.** If step 1 created a fresh AGENTS.md (repo had none), it has
   the harness section but no org charter: prepend the relevant sections (Standards/gate
   for Python apps, Deploy incl. the deploy-tools list, Layout) adapted from
   `${CLAUDE_PLUGIN_ROOT}/template/python-backend/AGENTS.md` (or `template/node/AGENTS.md`),
   with this repo's real name and layout. If the repo already had an AGENTS.md, just make
   sure its Deploy section mentions `agent/tools/deploy-{validate,status,logs}`.

4. **Tailor `agent/manifest.yml`** — real entrypoints, important paths, frameworks.

5. **Validate the deploy files:**
   ```bash
   ./agent/tools/deploy-validate
   ```
   Fix what it flags (this is how old repos with leftover placeholders or three-segment
   images get caught — e.g. the known `zwischen-uns` placeholder). Re-run until
   `DEPLOYABLE`.

6. **Report and stop.** Show what was installed/changed and the suggested commit:
   ```bash
   git add -A && git commit -m "feat: carry repo-agent-harness + deploy tools in-repo"
   ```
   Do NOT push automatically.
