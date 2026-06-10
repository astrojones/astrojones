# nuklaut deploy — failure modes and fixes

Map a symptom to its cause. `deploy-doctor` automates this against a real run's logs.

| Symptom | Cause | Fix |
|---------|-------|-----|
| CI step "Build/Push image" ok, but app 502s; `nuk list` shows no running container | Compose pulls a different image path than CI pushed | Use two-segment `ghcr.io/astrojones/<repo>:latest` in compose (rule 1) |
| `manifest not found` / workflow fails early | No `.nuklaut/deployment.yml`, or wrong path | Add it at repo root; default path is `.nuklaut/deployment.yml` |
| `docker compose` error: port already allocated | `ports:` in compose | Replace with `expose:` |
| App deploys but 404 at Traefik, or duplicate routers | Hand-written `traefik.*` labels | Remove them; nuk generates routing from `spec.ingress` |
| Container name conflict on redeploy | `container_name:` set | Remove it |
| Image is `…/__REPO_NAME__/…`, pull fails | Placeholder never replaced | `grep -rl __REPO_NAME__ .` and replace with the repo name |
| App starts but env vars empty | `APP_ENV` secret not set, or wrong key names | Add/fix the `APP_ENV` repo secret (Settings → Secrets → Actions) |
| `DATABASE_URL` unset despite `databases.postgres: true` | No `spec.ingress` entry (db attaches to ingress services) | Declare at least one ingress entry |
| SQLAlchemy/asyncpg can't parse `DATABASE_URL` | Bare `postgresql://` scheme | Use `postgres: { driver: asyncpg }` |
| Redis writes rejected (NOPERM) | Keys not namespaced | Prefix all keys with `<app>:` |
| Old `deploy.yml` points at a stale workflow path | Repo predates current reusable workflow | Point `uses:` at `astrojones/.github/.github/workflows/nuk-deploy.yml@main` |

## Quick self-check before pushing

```bash
# no leftover placeholders
grep -rn '__REPO_NAME__' . && echo "FIX THESE" || echo "ok: no placeholders"
# image is two-segment
grep -n 'image:' docker-compose.yml
# no forbidden compose keys
grep -nE 'ports:|container_name:|traefik\.' docker-compose.yml && echo "REMOVE THESE" || echo "ok: compose clean"
# manifest name matches the repo
grep -n 'name:' .nuklaut/deployment.yml
```

## Inspecting a live deploy

You don't have controller SSH; ask an admin (or deploy-doctor reads CI). Admin-side:

```bash
nuk list           # is the container up? what host?
nuk logs <repo>    # app logs
```

CI side (you can do this):

```bash
gh run list --repo astrojones/<repo> --workflow deploy.yml -L 5
gh run view <run-id> --repo astrojones/<repo> --log-failed
```
