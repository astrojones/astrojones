# astrojones-dev

Claude Code plugin that makes shipping apps in the `astrojones` org easy. It bundles
the deployment knowledge, a one-command scaffolder, and a CI-failure diagnostician so
a new contributor can go from nothing to a live `https://<app>.astrojones.de` without
learning the controller internals or touching SSH.

## Install

```bash
/plugin marketplace add astrojones/astrojones-dev
/plugin install astrojones-dev@astrojones
```

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

## Born harnessed — apps that teach any coding assistant

Every scaffolded repo also carries the **[repo-agent-harness](https://github.com/astrojones/agentic-repo)**
(installed at scaffold time via `uvx … repo-agent-harness init`, sha-pinned): `AGENTS.md`
(org charter + harness rules), `.mcp.json` (Serena + 10 deterministic `repo_*` tools),
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
