# astrojones-dev

Claude Code plugin that makes shipping apps in the `astrojones` org easy. It bundles
the deployment knowledge, a one-command scaffolder, and a CI-failure diagnostician so
a new contributor can go from nothing to a live `https://<app>.astrojones.de` without
learning the controller internals or touching SSH.

## Harness MCP server — what changed

The per-repo workflow skills (`bugfix`, `feature`, `refactor`, `test`, `implement`,
`commit-semantic`) and the `/harness-init` workflow now live in the harness MCP server
as **prompts** (the single source of truth). They're exposed two ways:

- `@mcp.prompt()` registration on the server, so any MCP-aware client that surfaces
  prompts (Claude Code) discovers them via `prompts/list` and reads them via
  `prompts/get`.
- A `repo_prompt_get(name)` MCP tool wrapper, for clients that only surface tools
  to the model (notably opencode).

The plugin repo's `skills/<name>/SKILL.md` files remain as offline copies for
Claude users who run without an MCP connection; a drift check (`repo_drift_check`
MCP tool / `repo-agent-harness drift-check` CLI) reports divergence as a
**warning, never an error**, and `repo_drift_sync` / `repo-agent-harness
sync-prompts` refreshes them.

The `/harness-init` command now delegates to the new `bootstrap --target both` CLI
subcommand (the actual first-touch materialization — `agent/`, `AGENTS.md`,
`.mcp.json` with `--pin`, and `.opencode/opencode.json` for opencode users).
The plugin's load-time hook is expected to invoke this automatically; the command
is the explicit re-bootstrap path.

The deploy validators (`agent/tools/deploy-validate`, `deploy-status`, `deploy-logs`)
are now also exposed as MCP tools (`repo_deploy_validate`, `repo_deploy_status`,
`repo_deploy_logs`) and CLI subcommands, so the model can invoke them directly
without spawning a subprocess from a tool wrapper. The shim files in
`template/_shared/agent/tools/` stay for the non-plugin / CI case.

The org-only knowledge (`nuklaut-deploy` skill, `deploy-doctor` agent, `/new-app`
and `/harness-app` commands) is out of scope for this change — it stays Claude-only
in this repo. A separate org plugin is the planned home for it.

## Install

```bash
/plugin marketplace add astrojones/claude-plugins
/plugin install astrojones-dev@astrojones
```

(The org plugin index lives in [`astrojones/claude-plugins`](https://github.com/astrojones/claude-plugins);
this repo is just the plugin.)

(Requires org membership — the repo is public, but `gh repo create` inside the org
needs you to have accepted your org invite.)

## What's inside

| Component | Kind | What it does |
|-----------|------|--------------|
| `nuklaut-deploy` | Skill | Always-on knowledge. Auto-triggers when you edit `.nuklaut/deployment.yml`, a compose file, or ask "how do I deploy". Knows the `nuk/v1` schema, the hard compose rules, two-segment GHCR naming, the `APP_ENV` secrets model, databases, ingress, and auth. |
| `/new-app <name>` | Command | Scaffolds a new app — **Python (FastAPI) by default**, or `--node`. For Python it generates `pyproject.toml` from the org [standards](https://github.com/astrojones/standards) and proves `uv sync`+`pytest`+`ruff`+`ty` pass before handing off. Creates the repo, wires the deploy files, replaces placeholders, **harnesses the repo** (see below), and gates on `deploy-validate`. Does not push — you decide when. |
| `/harness-app <name>` | Command | Retrofits an existing org app to the same standard: installs the harness, copies the deploy tools, completes AGENTS.md, runs `deploy-validate`. |
| `deploy-doctor` | Agent | Diagnoses a red `deploy` run or a 502: runs `deploy-validate` for the mechanical checks, pulls the run logs, and maps the failure to a root cause + concrete fix. |
| `template/_shared/agent/tools/` | Repo-carried tools | `deploy-validate` (the hard rules as a tested checker), `deploy-status`, `deploy-logs` — copied into every app, runnable by any assistant or CI, no SSH. Tested in this repo: `uv run pytest`. |
| `bugfix`, `feature`, `refactor`, `test`, `implement`, `commit-semantic` | Skills | The [repo-agent-harness](https://github.com/astrojones/repo-agent-harness) coding workflows (the harness repo itself is plugin-free; its Claude Code surface lives here). |
| `implementer`, `reviewer`, `test-runner` | Agents | The harness workflow subagents (TDD streams, diff review, narrow verification). |
| `explorer` | Agent | Read-only Serena-first symbol navigation and deep exploration of a code region — two modes (scout breadth / explore depth), hard read budget, cited summaries, no full-file dumps. Hands edits to `implementer`. |
| `fullstack-architect` | Agent | Designs and builds UI⇄backend vertical slices as one typed contract — elegant, lean, witty interfaces wired to real FastAPI/pydantic APIs, incl. tectonic/synctex document generation. Opinionated to the org stack (FastAPI + Svelte/SvelteKit + Tailwind/shadcn). |
| `hooks/` | Hooks | Safe-shell + secret-read guard piped through the *repo's own* sha-pinned harness (`repo-agent-harness hook pre-tool-use`, fail-open shim) + post-edit verification nudge. |
| `/harness-init` | Command | Scaffolds the harness (agent/, AGENTS.md, sha-pinned `.mcp.json`) into any existing repo — the generic version of what `/new-app` does automatically. |

## Born harnessed — apps that teach any coding assistant

Every scaffolded repo also carries the **[repo-agent-harness](https://github.com/astrojones/repo-agent-harness)**
(installed at scaffold time via `uvx … repo-agent-harness init`, sha-pinned): `AGENTS.md`
(org charter + harness rules), `.mcp.json` (one harness server: deterministic `repo_*`
tools + proxied `serena_*` navigation),
`agent/policies/` (safe-shell bounds), and `agent/tools/` (the same operations as CLIs).
A contributor can clone an app and point **any** MCP-capable assistant at it — vanilla
Claude Code, Codex, Cursor — with zero plugin installs: the repo itself provides repo
navigation, guarded shell, and the deploy loop (`deploy-validate` / `deploy-status` /
`deploy-logs`). This plugin stays the *creator-side* toolkit; the *repo* is the delivery
vehicle for everyone else.

## How deployment works (the short version)

Push to `main` → the org reusable workflow
(`astrojones/.github/.github/workflows/nuk-deploy.yml`) runs on the self-hosted
`nuklaut` runner → builds and pushes `ghcr.io/astrojones/<repo>:latest` → `nuk apply`
turns your manifest into a docker-compose project behind Traefik at
`https://<repo>.astrojones.de`. **No SSH, no per-repo deploy keys.**

Every app needs exactly four files:

```
.github/workflows/deploy.yml   # calls the reusable workflow; never edit
.nuklaut/deployment.yml        # nuk/v1 manifest — ingress, optional db, auth
docker-compose.yml             # service def; no ports/labels/container_name
Dockerfile                     # yours
```

The canonical, always-correct copies live in [`template/`](./template/) —
`template/python-backend/` (FastAPI + uv, wired to the org Python standard) and
`template/node/` (minimal Node service). That is what `/new-app` copies from.

## The rules that bite people

1. Image is **two-segment**: `ghcr.io/astrojones/<repo>:latest`.
2. **No `ports:`** in compose — use `expose:`.
3. **No `traefik.*` labels** — nuk generates routing.
4. **No `container_name:`**.
5. `metadata.name` **==** repo name.
6. Replace every **`__REPO_NAME__`** before pushing.

See the `nuklaut-deploy` skill (`skills/nuklaut-deploy/references/`) for the full
manifest reference and a symptom→fix troubleshooting table.

## Relationship to other org repos

- **`astrojones/.github`** — hosts the reusable CI workflows (deploy, runner
  lifecycle, e2e, migration checks). This plugin does **not** duplicate them; the
  scaffolded `deploy.yml` calls them. Keep editing CI there.
- **`astrojones/standards`** — the SSOT for engineering standards (Python tooling,
  CI/CD). `/new-app` and `pyproject-canon` generate from it; don't hand-copy its config.
- **`astrojones/app-template`** — the old "Use this template" repo. Superseded by
  `/new-app`; slated for retirement once the command is proven.

## Local development

```bash
claude --plugin-dir /path/to/astrojones-dev    # test before publishing
```
