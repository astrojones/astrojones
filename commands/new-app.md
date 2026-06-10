---
description: Scaffold a new astrojones app repo wired for nuklaut auto-deploy
argument-hint: <app-name> [--public]
allowed-tools: Bash, Read, Write, Edit, Glob
---

Create and wire up a new deployable app in the `astrojones` org. The app name is
`$1` (kebab-case). Visibility defaults to **private**; create public only if the user
passed `--public`.

The plugin's template files live at `${CLAUDE_PLUGIN_ROOT}/template/`. Use them as the
source of truth — do not hand-author the four files from memory.

Follow these steps exactly:

1. **Validate the name.** It must match `^[a-z][a-z0-9-]*$`. If `$1` is empty or
   invalid, stop and ask for a valid kebab-case name. Confirm it is not already taken:
   `gh repo view astrojones/$1` — if that succeeds, stop and report the repo exists.

2. **Create the repo** (empty, on GitHub) and clone it locally into the current
   directory:
   ```bash
   gh repo create astrojones/$1 --private --clone   # add --public only if requested
   ```

3. **Copy the template** into the clone, then **replace every `__REPO_NAME__`** with
   `$1`. Copy `${CLAUDE_PLUGIN_ROOT}/template/` (including the dotfiles
   `.github/` and `.nuklaut/`) into `./$1/`, then:
   ```bash
   cd $1 && grep -rl '__REPO_NAME__' . | xargs perl -pi -e "s/__REPO_NAME__/$1/g"
   ```
   (`perl -pi` is portable across macOS and Linux, avoiding the BSD/GNU `sed -i`
   split.) After replacing, verify none remain: `grep -rn '__REPO_NAME__' .` must
   print nothing.

4. **Sanity-check the scaffold** against the hard rules (load the `nuklaut-deploy`
   skill if not already): two-segment image in `docker-compose.yml`, no `ports:` /
   `traefik.*` / `container_name:`, `metadata.name == $1`.

5. **Tell the user the manual steps you cannot do for them**, clearly:
   - Replace `Dockerfile` with one that builds their app on port 8080 (or change the
     port consistently in `Dockerfile` EXPOSE, `docker-compose.yml` expose, and
     `.nuklaut/deployment.yml` port).
   - If the app needs secrets, add a repo secret named **`APP_ENV`** (multiline
     `key=value`) at Settings → Secrets and variables → Actions. Offer the command:
     ```bash
     gh secret set APP_ENV --repo astrojones/$1 < your-env-file
     ```
   - If the app needs a database, uncomment `spec.databases` in
     `.nuklaut/deployment.yml`.

6. **Do NOT push automatically.** Show the user what will happen on push (build →
   GHCR → `nuk apply` → `https://$1.astrojones.de`) and give them the command to
   deploy when ready:
   ```bash
   git add -A && git commit -m "feat: initial app scaffold" && git push -u origin main
   ```
   Pushing triggers the first deploy via the org `nuklaut` runner. After they push,
   suggest `/deploy-doctor` if the run goes red.

Keep output tight: report what you created, what remains for the user, and the deploy
command. Do not narrate each step.
