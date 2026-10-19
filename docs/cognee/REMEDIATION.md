# Cognee memory layer — remediation roadmap

**Status:** dormant on `main` behind the default-off master switch
`REPO_AGENT_HARNESS_COGNEE_ENABLE` (see `cognee_client.cognee_runtime_enabled()`).
On this branch, work on the fixes below with the switch armed:

```bash
export REPO_AGENT_HARNESS_COGNEE_ENABLE=1
```

Source: adversarial multi-agent review of the cognee integration
(`git diff 37328a1..0914ae8`, 77 files, +9323/−1065). 17 findings, each finder-verified
by a separate adversarial verifier. Severity-sorted. Fix `#1` first — it makes the whole
claude-mem→cognee mirror actually recallable.

---

## 🔴 Critical

**#1 — Mirror writes `cm_<project>`, every reader queries `<project>`/`agent_sessions` → mirror unrecallable**
`cognee_sync.py:75` (write) vs `mem.py:58` / `agent_hooks.py:432` (read).
CogneeSync ships all claude-mem docs into `cm_<project>` (node_set `session_digest`), but
`resolve_dataset` yields the bare `<project>` (onboarded) or `agent_sessions` (default). cognee
search is dataset-scoped and the node_set filters only *within* the queried dataset, so nothing
ever reads `cm_*`. The entire mirrored corpus lands in a write-only namespace, for every repo,
permanently. (Data not destroyed — claude-mem source untouched, docs recoverable in `cm_*`.)
**Fix:** unify the namespace — ship into the onboarded/default dataset, drop the `cm_` prefix,
keep the `session_digest` node_set as the origin filter (the `cm_` split is redundant with the
node_set and is the bug's root cause). Then re-point/reset watermarks so the backlog re-ships
into the correct dataset.

## 🟠 High

**#2 — `status()` reports `embedding_key_present: true` with no real key** — `cognee_local.py:204`.
Container always gets the non-empty placeholder `or-local-no-key` (`:420`), so the `test -n` probe
always succeeds → the one diagnostic for the missing-key mode gives a false all-clear.
**Fix:** probe for a *real* key (compare against the placeholder), or have the container expose a
distinct "no key" marker.

**#3 — No-key auto-boot comes up "healthy" and silently degraded** — `cognee_local.py:420`.
Placeholder key + `COGNEE_SKIP_CONNECTION_TEST=true` (`:426`) suppress cognee's preflight;
`ensure_local` only checks `/health`; embedding calls 401 in the background, logged only at DEBUG.
**Fix:** when no real key is present, refuse auto-start (or emit a loud LOG.warning) instead of
booting a degraded stack; do not disable the preflight silently.

## 🟡 Medium

**#4 — `cm_<basename>` collapses distinct same-named repos into one namespace + ledger scope** — `cognee_sync.py:150`.
`~/work/api` and `~/side/api` → both `cm_api`. Cross-repo memory bleed. `paths.repo_id` (collision-free)
exists but is unused for the dataset. **Fix:** key the dataset by `repo_id` (or a project override), not bare basename.

**#5 — Cross-process double-ship: check-then-act on the machine-global ledger** — `cognee_sync.py:224`.
Two sessions on the same cwd = two processes, same dataset + same `sync_ledger.db`; both pass
`already_ok()==False` and ship the same batch (no unique constraint on `SyncItem`). Graph duplication
resting on unverified cognee-side dedup. **Fix:** add a unique constraint on `(dataset, kind, content_hash)`
and/or a cross-process lock around read→ship→record.

**#6 — Client 4xx trips the shared transport breaker → blocks all memory ops** — `cognee_client.py:314`.
`record_failure()` fires for every non-2xx incl. 400/404/422 (service is up). 5 consecutive client
errors open the breaker 120s, blocking legitimate ops on the process-singleton client.
**Fix:** only count 5xx / transport errors toward the breaker; treat 4xx as terminal caller errors.

**#7 — I2 invariant test only greps literal `subprocess`/`claude_agent_sdk`** — `test_invariants.py:33`.
`os.system`/`os.popen`/`os.exec*`/`os.posix_spawn` would pass green. **Fix:** scan for the os-spawn
primitives too (AST or token list).

**#8 — `provision_user` (local-cognee auth bootstrap) has zero test coverage** — `cognee_local.py:512`.
Monkeypatched to `True` in every test. **Fix:** cover login-ok / login-fail→register→relogin / HTTPError→False.

**#9 — `ensure_postgres` pull/run/never-ready branches untested** — `cognee_local.py:341`.
Only the network-fail return is tested; a regressed never-ready abort would boot cognee against a dead DB.
**Fix:** add tests for the three False branches.

**#10 — E2E S4 asserts a removed subsystem (`capture_queue.db`), zero real Stop-hook coverage** — `docker/e2e_verify.py:142`.
The local capture queue was removed in D5; no Stop hook exists. The unsatisfiable hard-check + the
unverified degraded-path arming give false confidence. **Fix:** delete/replace S4 with a real degraded-path
assertion (breaker trips, bounded latency).

**#11 — E2E S5 hard-check only proves the hook ran, not that a delta was injected** — `docker/e2e_verify.py:149`.
`perception_last_seen.json` is written unconditionally (`agent_hooks.py:202`) before the digest branch.
**Fix:** deterministically force a delta and hard-assert the injected `additionalContext`.

**#12 — E2E S2 never hard-asserts the injected context** — `docker/e2e_verify.py:116`.
Only the touched-path side effect is hard; the actual additionalContext payload is soft.
**Fix:** force a deterministic red/no-snapshot state and hard-assert the nudge/warning.

## 🟢 Low

**#13 — Non-atomic RMW on the per-repo touched-file across concurrent hook processes** — `agent_hooks.py:122`.
`_write_json` uses `write_text` (no temp+`os.replace`); last-writer-wins + torn reads. **Fix:** atomic write like `stamp_hook_heartbeat`.

**#14 — `sync_ledger` missing from the I2 scan module list** — `test_invariants.py:28`. On the sync path but unscanned. **Fix:** add it (and `paths`).

**#15 — OpenRouter key in the `docker run` argv in plaintext** — `cognee_local.py:487`. Visible via `ps`/`docker inspect`. **Fix:** use `-e LLM_API_KEY` passthrough (no inline value).

**#16 — `CogneeAuth` login/auth-failure path entirely untested** — `cognee_client.py:156`. **Fix:** cover bad-creds, no-token, transport-fail, persistent-401.

**#17 — Success-path JSON decode unguarded/untested** — `cognee_client.py:330`. Malformed 2xx body throws a raw `JSONDecodeError` past the `CogneeError` contract. **Fix:** wrap + raise `CogneeError`; add a test.

---

### Additional (author-flagged, own read)
- Sync completion parsing (`_extract_dataset_id`/`_extract_status`, `cognee_sync.py:288`) is FLAGGED as
  unverified against the live cognee OpenAPI. Wrong field shapes → `_poll` never sees `COMPLETED` →
  `timeout` → watermark stalls → same batch re-ships each cycle (then every ~10 min after the breaker),
  DEBUG-only. Verify against the live API and add a contract test.
- `mem_remember` returns success after `/add`, before the background `cognify` verifies — a failed
  extraction leaves the fact stored-but-unextracted while the caller believes it's remembered.

### Order
`#1` → `#2`/`#3` → `#5`/`#6` → then the untested failure paths (`#8`/`#9`/`#10`) before re-arming
cognee on `main`.
