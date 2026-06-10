# nuklaut admin reference (controller operator only)

App authors deploy via CI and never touch the controller. This file is for the
**controller operator** — manual `nuk` over SSH, Traefik internals, and the
provisioning guardrails. (Salvaged from the retired `heitech-deploy` skill.)

## Manual deploys: always via `deploy.sh`, never raw `nuk`

`nuk` runs *on* the controller. From the workstation, go through the wrapper —
never invoke `nuk` directly (it will fail with `/opt/nuklaut does not exist`).

```bash
cd ~/nuklaut/terraform
./deploy.sh nuk apply <manifest.yml>   # create/update (manifest scp'd, then applied)
./deploy.sh nuk list                   # deployments, container state, ingress hosts (aliases: get, ls)
./deploy.sh nuk logs <name>            # follow compose logs
./deploy.sh nuk rollout <name>         # git pull + compose pull + up -d
./deploy.sh nuk delete <name>          # compose down -v + remove dir + drop ingress (KEEPS env file)
```

Other wrapper commands: `./deploy.sh status` (docker ps + runner), `./deploy.sh logs`
(Traefik logs), `./deploy.sh ssh` (shell as `deploy`). SSH host auto-resolves from
`terraform output -raw server_ipv4`; override with `SSH_HOST=<ipv4>`.

## Writing a per-app secret by hand (operator path)

App authors use the `APP_ENV` repo secret instead. The operator can also write
directly:

```bash
IP=$(cd ~/nuklaut/terraform && terraform output -raw server_ipv4)
ssh deploy@$IP 'sudo install -m 600 -o deploy -g deploy /dev/stdin /opt/nuklaut/secrets/<repo>.env' <<'EOF'
DATABASE_URL=postgres://...
EOF
```

Mode 600, owned by `deploy`, never committed. **`nuk delete` does not remove it** —
delete by hand if you really mean to. Missing `secretRef` files are silently
skipped, so write the env file *before* the first apply or the container starts
with an empty environment.

## Traefik internals (why routing breaks)

For each `spec.ingress` entry, `nuk` writes `/opt/nuklaut/traefik/dynamic/<name>.yml`
with a router + service whose backend URL is `http://<name>-<service>-1:<port>`.

- `<name>-<service>-1` is Compose's **default** container name. If the compose file
  sets `container_name:`, the real container has a different name and the route
  **502s**. Remove `container_name:`.
- A 404 (not 502) means the dynamic file was generated but the container isn't on
  the `edge` network — usually the `ingress[].service` value doesn't match the
  compose service name exactly. `nuk` writes a `compose.override.yml` that joins
  every ingress service to external network `edge`; keep your compose Traefik-agnostic
  (no `edge` network declaration, no `traefik.*` labels).
- The override is auto-generated and clobbered on every apply — never hand-edit it.
- All ingress is HTTPS via Let's Encrypt **DNS-01**. A "Fake LE" / wrong cert means
  the Hetzner DNS token in `/etc/nuklaut/secrets/traefik-env` is missing/invalid, or
  Traefik is on the ACME staging endpoint. Check `./deploy.sh logs`.
- Wildcard `*.astrojones.de → controller` DNS already exists, so any
  `<repo>.astrojones.de` host works with no DNS change. Custom domains need an A record.

## `--repo-path` (how CI avoids git auth)

`nuk apply --repo-path <dir> <manifest>` skips the git clone and uses `<dir>` as the
checkout. The reusable workflow passes `$GITHUB_WORKSPACE`, so private repos need no
git credentials on the controller. On `--repo-path` deploys there's no `.git` dir, so
`nuk rollout` skips `git pull` — re-trigger the workflow to update the tree.

## Provisioning guardrails (do NOT break these)

- **Never `terraform destroy hcloud_server.controller` or `terraform taint` it.** The
  CX33 supply is sparse. In-place reimage (`./deploy.sh rebuild` / `bootstrap`, both
  idempotent) is the only acceptable reset path.
- **Traefik static/dynamic config** lives in `~/nuklaut/terraform/files/traefik/`. Edit
  there and re-bootstrap; don't edit on the server.
- **Runner registration token** is an admin-only step — see `terraform/README.md`.
