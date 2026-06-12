---
description: Scaffold a new astrojones app repo wired for nuklaut auto-deploy
argument-hint: <app-name> [--python|--node] [--public]
allowed-tools: Bash, Read, Write, Edit, Glob
---

Create and wire up a new deployable app in the `astrojones` org. App name is `$1`
(kebab-case). Visibility defaults to **private** (`--public` to override). Stack
defaults to **python** (a standard-compliant FastAPI backend); pass `--node` for
the Node stack.

Template files live under `${CLAUDE_PLUGIN_ROOT}/template/`:
- `template/python-backend/` — FastAPI + uv, wired to the org Python standard
- `template/node/` — minimal Node service

Use them as the source of truth — do not hand-author the four deploy files.
If not already loaded, load the `nuklaut-deploy` skill for the deploy rules.

Steps:

1. **Validate the name.** Must match `^[a-z][a-z0-9-]*$`. If `$1` is empty/invalid,
   stop and ask. Confirm it's free: `gh repo view astrojones/$1` succeeding means it
   exists — stop and report. Derive the Python package name as `$1` with hyphens →
   underscores (call it `PKG`).

2. **Create + clone the repo:**
   ```bash
   gh repo create astrojones/$1 --private --clone   # --public only if requested
   ```

3. **Copy the chosen template** into `./$1/` (including dotfiles `.github/`,
   `.nuklaut/`, `.dockerignore`, `.gitignore`), then overlay the shared deploy tools:
   ```bash
   TEMPLATE="${CLAUDE_PLUGIN_ROOT}/template/python-backend"  # or node/
   cp -r "${TEMPLATE}/." "$1/"                               # includes dotfiles
   cp -r "${CLAUDE_PLUGIN_ROOT}/template/_shared/." "$1/"    # agent/tools/deploy-*
   cd "$1"
   chmod +x agent/tools/*
   ```
   Replace placeholders:
   ```bash
   grep -rl '__REPO_NAME__' . | xargs perl -pi -e "s/__REPO_NAME__/$1/g"
   ```
   For the **python** stack, also rename the package dir and replace `__REPO_PKG__`
   (using the `PKG` name derived in step 1):
   ```bash
   mv src/__REPO_PKG__ "src/${PKG}"
   grep -rl '__REPO_PKG__' . | xargs perl -pi -e "s/__REPO_PKG__/${PKG}/g"
   rm -f PYPROJECT.md                    # the generation note; not part of the app
   ```
   Verify no placeholders remain: `grep -rn '__REPO_NAME__\|__REPO_PKG__' .` prints nothing.

4. **Python stack only — generate the standard tooling and prove it's green.**
   This is the whole point: the app must meet the org standard before it ships.
   - Generate `pyproject.toml` via the **`pyproject-canon`** skill: fetch the live
     canonical from `astrojones/standards/python/pyproject.canonical.toml`, apply the
     **api** shape (FastAPI/uvicorn/httpx + the `FAST` ruff rule) and Python **3.14**
     (`target-version = py314`), and substitute the project name (`$1`) and package
     (`PKG`). If `uv sync` can't resolve a dep on 3.14 yet, fall back to 3.13 — update
     `requires-python`, `target-version`, `.python-version`, and the Dockerfile `FROM`
     together so local, CI, and prod stay aligned.
   - Then run the gate and do not finish until it is clean:
     ```bash
     uv sync && uv run pytest && uv run ruff check . && uv run ty check
     ```
     If anything fails, fix it (it's a fresh standard scaffold — failures mean a
     placeholder or generation slip, not a tooling problem) before continuing.

5. **Harness the repo** so any MCP-capable coding assistant (not just Claude Code with
   plugins) gets safe, repo-aware tooling. One pinned command — bump the sha together
   with a tested harness update only:
   ```bash
   HARNESS_SHA="ebff259fb41b4db6fefdcfda549303d08e20868f"   # repo-agent-harness main sha; keep --from and --pin in sync
   uvx --from "git+https://github.com/astrojones/repo-agent-harness@${HARNESS_SHA}#subdirectory=mcp" \
     repo-agent-harness init --pin "${HARNESS_SHA}" --json
   ```
   This installs `agent/` (policies + manifest + health config + harness tools), writes
   `.mcp.json` (a single sha-pinned harness MCP server — Serena is proxied through it as
   `serena_*` tools), and appends the harness section to the scaffolded `AGENTS.md`.
   Report `created`/`merged`/`skipped`/`removed`. Then tailor
   `agent/manifest.yml` (entrypoints, important paths) for the new app.

6. **Validate the deploy files** with the repo's own deterministic checker — this
   replaces eyeballing the hard rules:
   ```bash
   ./agent/tools/deploy-validate
   ```
   It must end `DEPLOYABLE`. It checks: no leftover placeholders, two-segment image
   matching the repo name, no `ports:`/`traefik.*`/`container_name:`, manifest name ==
   repo, ingress↔compose service/port consistency, and the reusable-workflow ref.
   Fix anything it flags before continuing.

7. **Tell the user the manual steps:**
   - Replace the app logic (python: build out `src/PKG/`; node: replace the Dockerfile/app).
   - Secrets → add a repo secret `APP_ENV` (multiline `key=value`):
     ```bash
     gh secret set APP_ENV --repo astrojones/$1 < your-env-file
     ```
   - Database → uncomment `spec.databases` in `.nuklaut/deployment.yml`.

8. **Do NOT push automatically.** Show what push triggers (build → GHCR → `nuk apply`
   → `https://$1.astrojones.de`) and give the command:
   ```bash
   git add -A && git commit -m "feat: initial app scaffold" && git push -u origin main
   ```
   After pushing, `agent/tools/deploy-status` shows the run and the live URL;
   `agent/tools/deploy-logs` prints failed steps. Suggest the `deploy-doctor` agent if
   the first run goes red.

Keep output tight: report stack chosen, gate result (python), harness + validate
results, what you created, what remains, and the deploy command. Don't narrate each step.
