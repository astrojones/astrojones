---
name: astrojones-cognee-doctor
description: Use when durable memory misbehaves in a repository that has the repo-agent-harness — mem_search returns nothing or errors, mem_remember seems to vanish, recall is missing at session start, or you suspect double-written memory. Runs the checkable diagnosis (mem_doctor) and walks the known non-obvious cognee deployment gotchas.
---

# cognee-doctor — diagnose the durable-memory stack

1. **Run `mem_doctor`** (the checkable half). Read the verdict:
   - `configured: false` → the environment lacks `COGNEE_BASE_URL` plus `COGNEE_USER_EMAIL`/`COGNEE_USER_PASSWORD` (or `COGNEE_API_KEY`). Fix the env of the *server process*, then restart the MCP server.
   - `reachable: false` → the deployment itself is down or unreachable from this machine. Check the host, DNS, and any VPN before touching code.
   - `authenticated: false` (but reachable) → credentials are wrong or the user is missing server-side. Verify with a manual form login against `/api/v1/auth/login`.
   - `hints` naming **claude-mem** or the **cognee-memory plugin** → a competing capture pipeline is still live and memory is being double-written. Disable that plugin (the harness owns capture + recall now) — do not ignore this.

2. **Writes accepted but never searchable?** `mem_remember`/`mem_ingest` return `queued: true`, yet the fact never shows up: the background cognify likely failed. Check `mem_stats(dataset)` — a pipeline status other than `DATASET_PROCESSING_COMPLETED` means extraction is still running or errored; give a large ingest minutes, not seconds (eventual consistency is normal, not a bug).

3. **Server-side configuration gotchas** (narrative — not checkable from here):
   - cognee reads its configuration from the **`/app/.env` file inside the container**, NOT from process environment variables. After editing that `.env`, the container must be recreated: `docker compose up -d --force-recreate`. A plain restart is not enough.
   - A **"graph-health" error at container boot is benign** — known noise, not the reason your search fails.
   - New datasets can hit a first-cognify race (`CREATE TABLE graph_node` / pg_type). `mem_ingest` already works around it (serial-first); if a manual first write hit it, simply re-run the cognify.

4. **Escalation order**: env → `mem_doctor` → `mem_stats` → server logs on the deployment host. Never "fix" a memory problem by writing the same fact repeatedly — every retry that half-succeeded is a duplicate in the graph.
