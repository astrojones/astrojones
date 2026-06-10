# nuk/v1 manifest — full reference

Canonical schema lives in [`astrojones/nuk`](https://github.com/astrojones/nuk#manifest-nukv1).
This is the org-applied subset, with the patterns you actually need.

## Top-level shape

```yaml
apiVersion: nuk/v1
kind: Deployment
metadata:
  name: <repo>            # MUST equal the repo name
spec:
  source: {}              # leave empty; CI uses --repo-path on the runner checkout
  compose: {}             # optional; only to point at a non-default compose file
  ingress: []             # one entry per public route
  envFrom: []             # secret files to inject
  databases: {}           # optional managed Postgres / Redis
```

## `spec.ingress[]`

Each entry produces one Traefik router + service (TLS via the `letsencrypt` resolver).

```yaml
ingress:
  - host: my-app.astrojones.de   # wildcard *.astrojones.de DNS already resolves
    service: web                 # name of a service in docker-compose.yml
    port: 8080                   # the port that service listens on internally
```

**Path split** — route `/api` to a backend and `/` to a frontend on one host:

```yaml
ingress:
  - host: my-app.astrojones.de
    service: api
    port: 8000
    path: /api
  - host: my-app.astrojones.de
    service: web
    port: 8080
    path: /
```

**OAuth gate** — require GitHub org login (via the shared oauth2-proxy):

```yaml
ingress:
  - host: my-app.astrojones.de
    service: web
    port: 8080
    auth: oauth
```

DB access attaches to ingress services, so declare at least one ingress entry even
for an internal-only API.

## `spec.envFrom[]`

```yaml
envFrom:
  - secretRef: /opt/nuklaut/secrets/<repo>.env    # per-app, written from APP_ENV
  - secretRef: /opt/nuklaut/secrets/_shared.env   # org-wide
```

Files are created by the reusable workflow on every deploy. The per-app file comes
from the repo's `APP_ENV` secret; if absent, an empty file is written (harmless).

## `spec.databases`

```yaml
databases:
  postgres: true              # bare libpq URL: postgresql://<app>:<pw>@nuk-postgres:5432/<app>
  # postgres: { driver: asyncpg }   # -> postgresql+asyncpg://...  (SQLAlchemy/asyncpg)
  redis: true                 # redis://<app>:<pw>@nuk-redis:6379/0
```

On apply nuk:
- creates a Postgres role+database named `<app>`, enables `vector`, and revokes
  CONNECT from every other role (cross-app isolation);
- creates a Redis ACL user restricted to keys/channels prefixed `<app>:`, with
  dangerous commands (e.g. `FLUSHALL`) removed;
- generates each password once into `/opt/nuklaut/secrets/<app>-db.env` (re-applies
  reuse it — no credential churn) and wires that file + the private `data` network
  onto your ingress service(s).

**Lifecycle:** `nuk delete <app>` keeps the database. `nuk db ls` shows managed dbs
and flags `RETAINED` ones with no deployment. `nuk delete <app> --purge` or
`nuk db purge <app>` is the only way to drop the data.

## `spec.compose`

Only needed if your compose file is not `docker-compose.yml` at repo root:

```yaml
compose:
  file: deploy/compose.prod.yml
```

## docker-compose.yml rules (recap)

```yaml
services:
  web:
    image: ghcr.io/astrojones/<repo>:latest   # two-segment; matches CI push
    restart: unless-stopped
    expose:
      - "8080"        # NOT ports:
    # no traefik.* labels, no container_name:
```

nuk writes a `compose.override.yml` that joins ingress services to the `edge`
network (and `data` + the `<app>-db.env` env_file when databases are requested), so
your compose stays Traefik- and DB-agnostic.

## Multi-service apps (advanced)

The simple reusable workflow builds **one** image, `ghcr.io/astrojones/<repo>:latest`.
Apps with separate backend/frontend images (e.g. `kolbe`, `nukview`) need a custom
build that pushes `ghcr.io/astrojones/<repo>/<service>:latest` per service and a
compose referencing those three-segment paths. That is outside the `/new-app` path —
start from an existing multi-service repo instead.

## What `nuk apply` does (so failures make sense)

1. Uses the runner's checkout (`--repo-path`); no git auth on the controller.
2. Provisions any requested databases.
3. Writes the override (networks, db env_file).
4. Writes `/opt/nuklaut/traefik/dynamic/<name>.yml` (router/service per ingress).
5. `docker compose -p <name> ... up -d`.

## Subcommands (admin, over SSH — you rarely need these)

```bash
nuk list                 # deployments, container state, ingress hosts
nuk logs <name>          # compose logs -f
nuk rollout <name>       # re-pull image + up -d (re-trigger CI for workflow-managed apps)
nuk delete <name>        # down + remove; KEEPS db
nuk delete <name> --purge   # also drop db/creds (DESTROYS DATA)
```
