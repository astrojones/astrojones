---
name: nuklaut-deploy
description: Use when deploying an app to the astrojones org or writing/editing a nuk manifest (.nuklaut/deployment.yml), docker-compose.yml, or deploy workflow for an astrojones repo — covers the nuk/v1 schema, the hard compose rules, GHCR image naming, the APP_ENV secrets model, and databases/ingress/auth patterns. Triggers on "deploy to astrojones", "nuklaut", "nuk apply", "deployment.yml", "astrojones.de".
---

# Deploying to the astrojones org (nuklaut)

The org runs **nuklaut**: one Hetzner controller with Traefik + the `nuk` CLI + an
org-wide self-hosted GitHub Actions runner. You push to `main`; CI builds an image,
pushes it to GHCR, and `nuk apply` turns your manifest into a docker-compose project
behind Traefik at `https://<repo>.astrojones.de`. **No SSH access is required.**

## The four files every app needs

A deployable app repo contains exactly these:

| File | Purpose |
|------|---------|
| `.github/workflows/deploy.yml` | Calls the org reusable workflow. **Never edit** beyond the rare input. |
| `.nuklaut/deployment.yml` | The `nuk/v1` manifest — ingress, optional DB, auth. |
| `docker-compose.yml` | Service definition. Subject to the hard rules below. |
| `Dockerfile` | Builds your app. The only file you always rewrite. |

`/new-app` scaffolds all four correctly. Prefer it over copying by hand.

## Check the rules mechanically first

Scaffolded apps carry `agent/tools/deploy-validate` (installed by `/new-app`; retrofit
older repos with `/harness-app`). It deterministically checks every hard rule below plus
manifest↔compose consistency — run it before pushing and after editing any deploy file:

```bash
./agent/tools/deploy-validate          # exit 0 + "DEPLOYABLE" or a list of violations
```

`agent/tools/deploy-status` and `agent/tools/deploy-logs` show the pipeline after a push
(gh-based, no SSH). Only reason about the rules by hand when the repo lacks the tools.

## Hard rules — violating any of these breaks the deploy

1. **Image name is two-segment:** `ghcr.io/astrojones/<repo>:latest`.
   NOT `ghcr.io/astrojones/<repo>/<repo>`. The reusable workflow builds and pushes
   exactly `ghcr.io/astrojones/<repo>:{sha,latest}`; your compose must pull the same.
   (Three-segment paths like `astrojones/<repo>/<service>` only exist for
   multi-service apps with custom CI — not the simple path.)
2. **No `ports:` in compose.** Traefik reaches your container over the internal
   `edge` network. Use `expose:` instead. Publishing a port bypasses TLS and the proxy.
3. **No `traefik.*` labels.** nuk generates the router/service from `spec.ingress`.
   Hand-written labels conflict and are ignored.
4. **No `container_name:`.** nuk runs compose with `-p <repo>`; a fixed name collides
   across deploys and breaks the `<project>-<service>-1` target Traefik routes to.
5. **`metadata.name` must equal the repo name.** The runner, secrets path, ingress
   host, and image name are all derived from it. Mismatch = nothing lines up.
6. **Replace every `__REPO_NAME__` placeholder.** A literal `__REPO_NAME__` left in
   compose (it has happened) means CI pulls a nonexistent image. Grep before pushing.

## Minimal manifest

```yaml
apiVersion: nuk/v1
kind: Deployment
metadata:
  name: my-app                     # == repo name
spec:
  source: {}                       # workflow checkout is used; leave empty
  ingress:
    - host: my-app.astrojones.de   # wildcard DNS already exists
      service: web                 # must match a service in docker-compose.yml
      port: 8080                   # the port your app listens on (also `expose:`d)
  envFrom:
    - secretRef: /opt/nuklaut/secrets/my-app.env    # per-app, from APP_ENV
    - secretRef: /opt/nuklaut/secrets/_shared.env   # org-wide
```

## Secrets — no SSH, ever

Two channels, both written to the controller by CI on every deploy:

- **Per-app:** add a repo secret named **`APP_ENV`** — a multiline `key=value` env
  file. The reusable workflow writes it to `/opt/nuklaut/secrets/<repo>.env`.
  ```
  DATABASE_URL=postgres://user:pass@host/db
  MY_API_KEY=sk-...
  ```
  Set via: Repo → Settings → Secrets and variables → Actions → New repository secret.
- **Org-wide:** managed by the admin as org Actions secrets, written to `_shared.env`.

Reference both via `spec.envFrom`. Never commit a `.env` to git.

## Databases — let nuk provision them

Add `spec.databases` and `nuk apply` creates an isolated Postgres role+db (pgvector)
and/or a namespaced Redis user on the shared data stack, injecting `DATABASE_URL` /
`REDIS_URL` into your ingress service automatically:

```yaml
spec:
  databases:
    postgres: true                 # or { driver: asyncpg } for SQLAlchemy/asyncpg
    redis: true                    # ACL user limited to keys prefixed "<app>:"
```

Your app just reads `DATABASE_URL` / `REDIS_URL` from env. Redis keys **must** be
namespaced `<app>:…`. See `references/manifest.md` for lifecycle and driver details.

## More patterns (path-split ingress, OAuth gate, custom compose file, multi-service)

See **`references/manifest.md`** for the full `nuk/v1` schema and these cases, and
**`references/troubleshooting.md`** for what each failure mode looks like and its fix.
Controller operators (manual `nuk` over SSH, Traefik internals, provisioning
guardrails) — see **`references/admin.md`**.

## When something fails

Run `./agent/tools/deploy-validate` first — it mechanically rules the file-level causes
in or out. Then the `deploy-doctor` agent pulls the failed run's logs
(`agent/tools/deploy-logs` shows them too) and maps them to a cause. Invoke it on a red
CI run rather than guessing. Common causes: unreplaced `__REPO_NAME__`, image-path
mismatch (rule 1), a `ports:`/`traefik.*` violation, missing manifest, or a typo'd
`APP_ENV` key.
