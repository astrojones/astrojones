# AGENTS.md — __REPO_NAME__

Project charter every agent reads before editing.

## The gate (must pass before every commit)

Keep the Docker build green — it is the deploy artifact:

```bash
docker build -t __REPO_NAME__:check .
```

Add `npm test` to this gate as soon as the app has tests.

## Deploy

Push to `main` → builds → `ghcr.io/astrojones/__REPO_NAME__:latest` → `nuk apply`
→ https://__REPO_NAME__.astrojones.de. Per-app secrets go in the `APP_ENV` repo
secret; never commit a `.env`.

The four deploy files: `.github/workflows/deploy.yml` (never edit — it calls the org
reusable workflow), `.nuklaut/deployment.yml` (nuk/v1 manifest), `docker-compose.yml`,
`Dockerfile`. Hard rules: two-segment image `ghcr.io/astrojones/__REPO_NAME__:latest`;
no `ports:` (use `expose:`), no `traefik.*` labels, no `container_name:` in compose;
`metadata.name` must equal the repo name.

### Deploy tools (`agent/tools/`, no SSH needed)

- `deploy-validate` — check the four deploy files against the hard rules. Run before
  every push; CI failures it can catch locally are placeholder/image/compose mistakes.
- `deploy-status` — recent deploy runs plus the app and image URLs (`gh` auth required).
- `deploy-logs` — failed steps of the latest deploy run (`--full` for the whole log).

## Layout

```
Dockerfile                 your app build (listens on 8080)
docker-compose.yml         service def (expose 8080, no ports/labels)
.nuklaut/deployment.yml    nuk manifest
agent/                     repo-agent-harness: policies + tools (see harness section below)
```
