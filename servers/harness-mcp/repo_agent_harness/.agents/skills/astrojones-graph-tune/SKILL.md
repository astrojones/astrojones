---
name: astrojones-graph-tune
description: Use when the cognee memory graph's entity types have gone noisy in a repository that has the repo-agent-harness — mem_search returns diluted or scattered results, the same concept appears under many ad-hoc type names, or after a large uncurated ingest. Designs a fixed type vocabulary as NamedIndividuals, pins it with mem_ontology, and verifies the collapse with a canary re-ingest.
---

# graph-tune — pin the memory graph's type vocabulary

The failure mode: cognee's extraction LLM invents a new entity type for every phrasing
("Service", "Backend Service", "API service", "microservice", …). Retrieval then scatters
across near-duplicate types and recall quality decays. The precedent run collapsed **79
ad-hoc types down to 5 fixed ones**. The fix is a small ontology of `owl:NamedIndividual`s
plus a paired extraction prompt — both generated from ONE dict so they cannot drift.

## Method

1. **Probe the noisy vocabulary.** Run 3–5 `mem_search` queries (`search_type=CHUNKS`)
   over the affected dataset for the domain's core concepts. Collect the entity/type names
   that come back. `mem_stats(dataset)` confirms the dataset and its pipeline state first.

2. **Design the fixed set.** From the probe, choose:
   - a **fixed type set** — aim for 3–7 types, never more than ~10 ("Service", "Artifact",
     "Decision", "Incident", "Person" is a typical shape);
   - **~10–15 NamedIndividuals** — the domain's recurring proper nouns, each mapped to
     exactly one type (e.g. `{"kolbe-api": "Service", "Zeugnis Raster": "Artifact"}`).
   Fewer, sharper individuals beat exhaustive lists: the resolver matches fuzzily (0.8),
   so one canonical name catches its variants.

3. **Pin it: `mem_ontology(individuals)`.** Returns `ontology_key`, whether it uploaded
   (hash-keyed — the same dict is a no-op), and the **paired prompt** ("type must be
   EXACTLY ONE of: …"). The upload alone changes nothing — it takes effect where step 4
   applies it.

4. **Canary re-ingest.** Take 2–3 representative documents and re-ingest them
   (`mem_ingest`, small, cheap — dry_run first out of habit) so extraction runs under the
   new ontology key and prompt. Do NOT bulk re-ingest the whole dataset yet.

5. **Verify the collapse.** Re-run the step-1 probes. Pass: the canary entities carry
   ONLY types from the fixed set, and previously-scattered concepts resolve to the pinned
   individuals. Report the before/after type count. Fail: widen the individuals list (a
   missed alias) or sharpen the type set — then repeat from step 3 (the edited dict gets a
   new key automatically).

6. **Only after a green canary**, schedule the full re-ingest of the dataset — that is a
   bulk operation with real cost: run it through `mem_ingest` with `dry_run=true` first
   and respect the confirm gate.

Never delete the old graph data as part of tuning — collapse forward via re-ingest, and
leave cleanup as an explicit, separate decision.
