---
name: astrojones-mem-ingest-wisely
description: Use when loading a batch of documents, notes, or history into the cognee memory graph in a repository that has the repo-agent-harness — docs folders, ADRs, session digests, incident writeups. Curates before ingesting, cost-checks with a dry run, tags with node_sets, and proves retrieval with three canary queries before declaring success.
---

# mem-ingest-wisely — curated, cost-gated bulk memory loads

Every ingested token passes through cognee's extraction LLM — raw dumps are the expensive
way to build a worse graph. Curate first, price it, tag it, then prove retrieval actually
works.

## Method

1. **Curate/digest.** Select what genuinely earns graph residency: decisions, contracts,
   incidents, architecture — not build logs or boilerplate. Long raw material gets
   digested into compact observations first (what/why/outcome, concrete identifiers kept).
   Split into items of roughly a few hundred to a few thousand tokens each.

2. **Cost check: `mem_ingest(items, dataset, dry_run=true)`.** Read `estimated_tokens`
   and `estimated_cost_usd`. If the estimate exceeds the limit, either curate harder or
   consciously accept it — never blindly pass `confirm=true` to silence the gate.

3. **Ingest with tags.** `mem_ingest(items, dataset, node_set=[…], confirm=<if needed>)`.
   Pick `node_set` by content category (`project_docs` for repo/architecture knowledge,
   `agent_actions` for session history, `user_context` for preferences). The harness
   handles the fresh-dataset cognify race automatically (serial-first) — no manual
   staging needed.

4. **Wait for extraction.** The cognify runs in background; `mem_stats(dataset)` shows the
   pipeline status. Give a large batch minutes — eventual consistency is normal, and
   re-submitting "because nothing shows up yet" creates duplicates.

5. **Three canary queries** — retrieval is the acceptance test, not the 200 response:
   - **historical**: a "what happened / when / what changed" question a new teammate
     would ask about the ingested material;
   - **relational**: a question whose answer must CONNECT two ingested items ("which
     service was affected by decision X?") — this is what the graph is for;
   - **onboarding**: the broad orientation question ("what is <project> and how is it
     structured?").
   Run each via `mem_search` (GRAPH_COMPLETION for the relational/onboarding ones,
   CHUNKS to verify raw presence).

6. **Pass/fail report.** Pass: all three canaries answer from the ingested content.
   Partial: raw chunks present but relations missing — the type vocabulary is probably
   noisy; run the `astrojones-graph-tune` skill and canary again. Fail (nothing found):
   check `mem_stats` pipeline state and `mem_doctor` before touching the data.

Never re-run a failed ingest blindly — every half-succeeded retry is a duplicate in the
graph. Diagnose first (`mem_doctor`, `mem_stats`), then act.
