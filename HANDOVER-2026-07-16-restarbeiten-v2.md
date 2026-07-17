# Handover — Restarbeiten v2 (2026-07-16)

Plan: `~/.claude/plans/concurrent-bouncing-meteor.md` (approved; alle 7 Review-Amendments eingearbeitet).
Geliefert auf `main`: 6 semantische Commits (`60d568b`…`5684ccf`), Suite 533 passed / 1 skipped.

## Was geliefert wurde (Kurzfassung)

| Paket | Ergebnis |
|---|---|
| 0 Capability-Probe | cognee **1.3.0**: memify/update/delete_data/dataset_data/improve/Schema-Endpoints/Markdown-Export vorhanden. **Kein COGX**, **kein Temporal-Flag**, TEMPORAL-Search serverseitig kaputt (`PostgresAdapter.collect_time_ids` fehlt). Doppelpunkt-node_sets OK. Kein Server-Upgrade nötig. |
| 1e Scrubbing | `secrets.redact` am Enqueue (vor Truncation, volle Config-Chain, lru_cache); Recall-Sanitizer (`<`→U+2039, C0/C1-Strip); `defaults/secrets.yml` mit Code-Builtins synchronisiert + Drift-Tripwire-Test. |
| 1 Heartbeat | `paths.HOOK_EVENTS`/`stamp_hook_heartbeat`/`read_hook_heartbeats` (atomar); `agent_hooks.dispatch()` dedupliziert beide Dispatch-Sites, stampt bei Erfolg, reicht Root durch; session_start-Degradationswarnung (count≥3, 7d, nie pre-compact); `repo_health.hook_heartbeats` (informationell, `ok` unberührt); `mem_doctor`-Stop-Hint. |
| 1b SDK-Provider | `ClaudeAgentSdkProvider` (SDK 0.2.120, `max_turns=1`, ohne `setting_sources` → Incident-Klasse strukturell ausgeschlossen), Default `claude-sdk`, Fallback-Exception-Liste → bestehender `--bare`-CLI-Provider. |
| 1c Observation-Format | `DigestObservation{type,title,facts,concepts,files}` (7+7-Vokabular, Structured Output, fail-open zu Plaintext); Drain shipped pro Tag-Kombination (`type:*`, `concept:*`) ins **Projekt-Dataset** (Marker) mit node_set `session_digest` (Alt-Bestand in `agent_sessions` bleibt eingefroren liegen). |
| 1f Client | 6 Wrapper: `memify`, `update_data`, `dataset_data`, `delete_data`, `improve`, `export_markdown` + FakeCognee-Spiegel; Konstantentabelle in `mem.py` als SSOT; `mem_remember` marker-aufgelöst; `run_memify` + memify post-ingest (Drain + `_ship`). |
| 1d migrate-claude-mem | `claude_mem_migrate.py` + CLI (`migrate-claude-mem`, `memify`); ro+immutable, 3 Granularitäten, generisches Content-Hash-Ledger, zentrales Kosten-Preflight. **Dry-Run real:** summaries-only 489 Docs/$1.42 · digest 665 Gruppen/$1.49 · raw 5590/$11.88. Ausführung NICHT erfolgt (User-Entscheid). |
| 2 Graph-Hygiene | Kein Doc-Chatter vorhanden — „Got it." kam aus der memory-nativen Schicht (`default_session` seit 12.7.). Backup → `forget(memoryOnly)` → Re-cognify. **Session-Start-Recall liefert jetzt echte Fakten.** |
| 3 Housekeeping | Wheel-Check: 3 SKILL.md im Wheel (kein force-include). `~/.claude/AGENT_DNA.md`-Cutover: Memory-Primary = harness `mem_search`, claude-mem = frozen archive. Globale CLAUDE.md: bewusst unverändert (lean-ctx ist Read-Cache, nicht Memory). |
| 4 E2E | Lokal PASS (dispatch→Heartbeats→Health, Queue-Redaction); live: doctor grün, remember→`astrojones` marker-aufgelöst, `run_memify` True + Heartbeat, **mem_rules nicht mehr leer**. Docker: deterministische Layer grün. |

## Nächste Schritte (in sinnvoller Reihenfolge)

### 1. Sofort / operativ
- **CI-Run des Push beobachten** (falls beim Ship nicht schon grün bestätigt) und danach **Plugin-Update auf das neue Release** auf diesem Host (`/plugin update`); erst dann stampen die echten Session-Hooks Heartbeats.
- **`servers/harness-mcp/.agents/` löschen** (untracked Streuartefakt; automatisierte Löschung war policy-geblockt): `rm -rf servers/harness-mcp/.agents/`.
- `.env`: frischer `CLAUDE_CODE_OAUTH_TOKEN` liegt drin (16.7.); der alte war revoked — TEST 4/5 im Docker brauchten ihn.

### 2. Serena-Memories-Migration (klären + umsetzen) ← explizit angefragt
Werkzeug existiert: `repo-agent-harness migrate-serena-memories [--dataset] [--dry-run] [--confirm]` (cli.py; shipped `.serena/memories/*.md` mit node_set `["project_docs", "repo:<name>"]`, Originale bleiben liegen).
Zu klären, DANN ausführen:
- **Pro Repo entscheiden**: Für astrojones lief die Migration bereits einmal (14.7., „Migrated 6 notes", Session `01ad34a4…`) — prüfen ob vollständig/aktuell (`mem_search` gegen `project_docs`-Inhalte; ggf. Re-Run — das Ledger-Pattern aus 1d hat migrate-serena-memories NICHT, Doppel-Ingest vermeiden bzw. cognee-Content-Dedup verifizieren).
- **Andere Repos** (kolbe, nuk, …): je `.serena/memories/` sichten (kuratieren! `mem-ingest-wisely`-Regeln), Ziel-Dataset = Projekt-Dataset des Repos, dry-run → confirm.
- **Ob die Originale dauerhaft bleiben**: Sie dienen als Serena-Gate-Marker (`serena_gate.is_onboarded`) — NICHT löschen, solange das Gate den Marker liest; ggf. Gate auf eigenen Marker umstellen (kleines Follow-up), dann erst Originale archivieren.
- Optional: `migrate-serena-memories` auf das generische Ledger aus `claude_mem_migrate.py` umstellen (Idempotenz nachrüsten, ~20 Zeilen).

### 3. claude-mem-Migration AUSFÜHREN (wenn Dataset-Strategie steht)
- Empfehlung aus dem Dry-Run: **digest-Granularität** (665 Session-Gruppen, ~$1.49) oder summaries-only ($1.42) — beide über dem $1-Gate: `COGNEE_INGEST_COST_LIMIT_USD=2` setzen und mit `--confirm` fahren; pro Projekt filterbar (`--project astrojones` zuerst als Pilot).
- Ziel-Datasets = jeweilige Projekt-Datasets (Ein-Dataset-pro-Projekt-Architektur); Ledger macht Wiederholungen idempotent.
- Danach: `/continue-claude-work` bleibt lokal-session-basiert (liest JSONL-Files, kein claude-mem) — AGENT_DNA ist bereits umgestellt.

### 4. Cognee-Server-Themen (aus Paket 2 offen)
- **CHUNKS-Index astrojones inkonsistent**: leer für spezifische Queries, 1 stale „Got it."-Orphan bei der generischen; `dataset_status` meldet ERRORED trotz funktionierendem Graph. Nächster Hebel: `improve` mit `buildGlobalContextIndex` laufen lassen bzw. Re-Ingest der 7 Docs (Raw-Records intakt; Backups unter `~/.harness/backups/cognee-astrojones-*.md`). Falls hartnäckig: cognee-Logs zur ERRORED-Pipeline ansehen.
- **TEMPORAL-Search kaputt** (`collect_time_ids` fehlt im Postgres-Adapter) — Upstream-Issue oder Adapter-Wechsel prüfen; bis dahin bleibt `Observed:` reine Zukunftsinvestition.
- **kolbe/agent_sessions tragen noch memory-native Chatter** („Got it." in mem_rules-Ergebnissen); gleiche Kur wie astrojones (Backup → `forget(memoryOnly)` → Re-cognify) — Achtung: `forget(memoryOnly)` resettet auch die Doc-Verarbeitung → Re-cognify einplanen.
- **mem_rules sucht ungescoped über alle Datasets** — ggf. Dataset-Scoping-Parameter nachrüsten (Zweizeiler im Client/mem).

### 5. Folgephase (aus Plan + Review, unverändert benannt)
- **Ontologie-Phase**: geteilte Dev-Observation-Ontologie empirisch aus dem 5590er-Korpus; die Schema-Endpoints (`infer-schema` → `update-dataset-schema`) geben dem graph-tune-Workflow eine API. Domain-Ontologie pro Projekt optional (kolbe-essential existiert).
- **Tier-B Code-Bridge** (code_map aus symbols.json): Upsert nativ via `update_data`-Wrapper (liegt bereit); Änderungserkennung mit dem generischen Ledger-Pattern.
- **Tier-C-Konsolidierung/Decay**: zuerst serverseitig (memify Entity-Consolidation + `improve` + Global Context Index, periodisch); eigener SDK-Rollup-Job nur wenn messbar nötig.
- **`repositories`-Routing-Dataset** + Cross-Projekt-Recall (akzeptierte Lücke bis dahin).
- **Recall-Eval** (1c-6): CHUNKS vs GRAPH_COMPLETION messen, sobald digestete Observations im Graph liegen (erst CHUNKS-Index fixen, s. 4).
- **Async-Job-Verkabelung**: CLI-Subcommands existieren (memify; weitere folgen) — launchd/cron bzw. post-merge-Trigger aufsetzen; Jobs stampen bereits in `heartbeats/`.

### 6. Kleinere technische Follow-ups
- `test_wal_survives_concurrent_writers` flaked unter Last (fail-open-Enqueue droppt bei busy-timeout; 23==24 im Container) — Test robuster machen oder Timeout erhöhen.
- Malformed-Regex in repo-`secrets.yml` killt Capture still (Row-Drop, unsichtbares debug-Log) — Validierung in doctor/health nachrüsten (Review-Should-fix, systemisch).
- `secrets.load` ist first-file-wins, kein Merge: ein Repo-`secrets.yml` mit eigenen Patterns ERSETZT die Builtins — Merge-Semantik erwägen (jetzt load-bearing am Capture-Pfad).
- `export_markdown`-Wrapper: Endpoint liefert rohes Markdown; `CogneeClient.request` parst JSON → für Paket-2-artige Nutzung raw-Variante nachrüsten (Hygiene-Skripte nutzten httpx direkt).
- Docker-`test.sh` ist nicht host-sicher (bei fehlgeschlagenem `cd` liefen `git add/commit -m init` + `user.name=t` im echten Repo — passiert am 16.7., zurückgerollt, Identität repariert): `set -e` um die cds bzw. Guard nachrüsten. **Immer über `docker/run.sh` starten.**

## Referenzen
- Backups: `~/.harness/backups/cognee-astrojones-20260716-122904.md` (175 KB, vor Hygiene) · `…-audit-20260716-124709.md` (nach Hygiene)
- Migrations-Ledger (bei Ausführung): `~/.harness/migrations/claude-mem-<dataset>.json`
- claude-mem-Store (frozen): `~/.claude-mem/claude-mem.db` (sha256 `4f32d9cc…`, unverändert verifiziert)
- Heartbeats: `~/.harness/repos/<id>/heartbeats/<event|job>.json`
