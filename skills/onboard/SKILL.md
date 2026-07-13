---
name: onboard
description: Use once per repository that has the repo-agent-harness to build its durable project memory in the cognee graph — the one-time onboarding that pins a type ontology, curates and cost-gates the initial memories (tech stack, commands, conventions, structure), ingests them under user confirmation, and marks the repo onboarded so future sessions stop nudging. Symbol navigation is already live from auto-onboarding; this skill seeds memory, not Serena. Invoked as `/astrojones:onboard`.
argument-hint: [repo-name-override]
---

# onboard — one-time durable-memory onboarding for a harness repo

Auto-onboarding already opened the Serena gate at MCP connect, so symbols are navigable the
moment a session starts. What it does **not** do is populate the repo's durable memory in the
cognee graph. This skill does that once: it pins a fixed type vocabulary **before** any ingest
(so extraction can't sprawl into ad-hoc types), curates a small set of high-value onboarding
memories, cost-gates and confirms the spend, ingests under a single ontology key, and records
that the repo is onboarded so later sessions stop prompting for it.

Run it once per repo. Re-running is safe (the ontology dict is hash-keyed, a no-op if
unchanged) but unnecessary — the completion flag is the signal to stop.

## Method

### 1. Orient
Call `repo_context_overview` (languages, entrypoints, important paths) and navigate by symbol
via `serena_get_symbols_overview` / `serena_find_symbol`. The Serena gate is already open from
auto-onboarding — no bootstrap call needed. Goal: identify the repo's core concepts — its
services/packages, key modules, produced artifacts, and load-bearing decisions.

### 2. Design an initial ontology
From what you found, design a small fixed vocabulary:
- a **fixed type set** — 3–7 types, never more than ~10 (a typical shape: `Service`,
  `Module`, `Artifact`, `Decision`, `Convention`);
- **~10–15 NamedIndividuals** — the repo's recurring proper nouns, each mapped to exactly one
  type (e.g. `{"repo-agent-harness": "Service", "AGENTS.md": "Artifact"}`).
Fewer, sharper individuals beat exhaustive lists: the resolver matches fuzzily (~0.8), so one
canonical name catches its variants.

### 3. Pin it FIRST — `mem_ontology(individuals)`
Pin the vocabulary **before** ingesting anything. Capture the returned `ontology_key` and its
paired `prompt` ("type must be EXACTLY ONE of: …"). Pinning first is the entire point: it stops
ad-hoc type sprawl at extraction time instead of forcing a graph-tune cleanup later. Use the
**same** `ontology_key` for every ingest below.

### 4. Build curated onboarding memories
Write compact, high-value items (a few hundred to a few thousand tokens each — like
`astrojones-mem-ingest-wisely`), not raw file dumps:
- **tech_stack** — languages, frameworks, runtimes, notable dependencies;
- **suggested_commands** — how to build/test/lint/run (from `agent/tools/*`, health config, task runners);
- **conventions** — code style, commit format, naming, testing method the repo actually uses;
- **structure/entrypoints** — the important paths and where execution starts.
Curate: what genuinely earns graph residency, not boilerplate.

### 5. Cost gate — dry run
`mem_ingest(items, dataset="agent_sessions", node_set=["project_docs", "repo:<name>"], ontology_key=<key>, dry_run=true)`.
Read `estimated_tokens` and `estimated_cost_usd`. If it's high, curate harder rather than
accepting blindly.

### 6. Confirm the spend — `AskUserQuestion`
Show the estimate and ask the user to approve before spending. **Never blindly pass
`confirm=true`** to silence the gate.

### 7. Ingest
`mem_ingest(items, dataset="agent_sessions", node_set=["project_docs", "repo:<name>"], ontology_key=<key>, confirm=<if needed>)`
with the **same** `ontology_key` from step 3. The harness handles the fresh-dataset cognify
race automatically. Extraction runs in the background — `mem_stats(dataset)` shows pipeline
state; give a batch minutes and don't re-submit (that duplicates).

### 8. Mark done — `repo_onboard_complete`
Call `repo_onboard_complete` with the `dataset` and `ontology_key`. This sets the per-repo flag
so future sessions stop nudging about onboarding.

### 9. Acceptance — three canary queries
Retrieval is the acceptance test, not the 200 response. Run three `mem_search` queries:
- **historical** — a "what is here / how does it work" orientation question;
- **relational** — a question whose answer must CONNECT two ingested items (GRAPH_COMPLETION);
- **onboarding** — the broad "what is <repo> and how is it structured?" (GRAPH_COMPLETION).
Pass: all three answer from the ingested content **and** the entity types collapse to the
pinned set. Partial (chunks present, relations missing): the vocabulary is noisy — run
`astrojones-graph-tune` and canary again.

## Note on prior manual onboarding

The repo may already carry `.serena/memories/*.md` from an earlier manual Serena onboarding.
This skill **may offer** to migrate that content into cognee via the `mem_*` tools (curate →
dry-run → confirm → ingest under the same `ontology_key`). It must **never** auto-delete disk
memories or graph data — cleanup is always an explicit, separate user decision.
