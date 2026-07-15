---
name: onboard
description: Use once per project (a project may span multiple repos that share one dataset) that has the repo-agent-harness to build its durable project memory in the cognee graph — the one-time onboarding that derives a type ontology from the repo's own symbol map and structure, curates and cost-gates the initial memories (tech stack, commands, conventions, structure), ingests them into the project dataset under user confirmation, and marks the repo onboarded so future sessions stop nudging. Also generates or updates the repo's `CLAUDE.md` (tech stack, commands, conventions, structure) as `/init` would, mirroring the same curated content into cognee memory. Symbol navigation is already live from auto-onboarding; this skill seeds memory, not Serena. Invoked as `/astrojones:onboard`.
argument-hint: [project-name] [--noninteractive]
---

# onboard — one-time durable-memory onboarding for a harness repo

Auto-onboarding already opened the Serena gate at MCP connect, so symbols are navigable the
moment a session starts. What it does **not** do is populate the repo's durable memory in the
cognee graph. This skill does that once: it pins a fixed type vocabulary **before** any ingest
(so extraction can't sprawl into ad-hoc types), curates a small set of high-value onboarding
memories, cost-gates and confirms the spend, ingests into the project's cognee dataset under a
single ontology key, and records that the repo is onboarded so later sessions stop prompting for it.

Run it once per **project** (a project may span more than one repo; they share one dataset).
Re-running is safe (the ontology dict is hash-keyed, a no-op if unchanged) but unnecessary — the
completion flag is the signal to stop.

## Method

### 1. Orient
Call `repo_context_overview` (languages, entrypoints, important paths) and navigate by symbol
via `serena_get_symbols_overview` / `serena_find_symbol`. The Serena gate is already open from
auto-onboarding — no bootstrap call needed. Goal: identify the repo's core concepts — its
services/packages, key modules, produced artifacts, and load-bearing decisions.

### 2. Derive the ontology from the repo itself
Don't hand-invent the vocabulary — **derive it from what the repo already exposes**, then enrich.
The individuals are mostly the repo's own proper nouns, and you already have the data from step 1:

- **the symbol map / overview** (`repo_symbols_overview`, or `serena_get_symbols_overview` per
  file) — the tree-sitter index is your primary source: package modules, classes, and the
  functions that name the repo's tools and entrypoints;
- **directory conventions** — `agents/*` → agents, `skills/*` → skills, `commands/*` → commands,
  the package/source dir → modules;
- **the manifest** (`plugin.json` / `package.json` / `pyproject.toml`) — the project/service name
  and its declared dependencies;
- **the exposed tool/API surface** — the MCP tools or public entrypoints the repo serves.

Map each derived proper noun to exactly one type from a **fixed 3–7 type set** (a typical shape for
a harness-style repo: `Service`, `Module`, `Tool`, `Agent`, `Skill`, `Artifact`, `Convention`).
Then **enrich as necessary**: add the load-bearing concepts that aren't literal symbols — external
services the repo depends on, cross-cutting conventions, key produced artifacts.

A self-describing repo yields a large individual set (dozens), and that's correct — dense coverage
of the repo's own names is what makes recall resolve them. Keep the *type* set small; let the
*individuals* be as many as the structure genuinely has. The resolver matches fuzzily (~0.8), so
one canonical name per concept catches its variants.

### 3. Approve, then pin FIRST — `mem_ontology(individuals)`
**Human-in-the-loop by default:** present the derived `{individual: type}` set to the user for
approval (`AskUserQuestion`) before pinning — they can correct a type, drop noise, or add a missing
individual. The ontology is the load-bearing decision of the whole onboarding, so a human signs off
on it by default. Skip this gate **only** when invoked with `--noninteractive`.

Then pin the approved vocabulary **before** ingesting anything. Capture the returned `ontology_key`
and its paired `prompt` ("type must be EXACTLY ONE of: …"). Pinning first is the entire point: it
stops ad-hoc type sprawl at extraction time instead of forcing a graph-tune cleanup later. Use the
**same** `ontology_key` for every ingest below.

### 4. Build curated onboarding memories and `CLAUDE.md`
Write compact, high-value items (a few hundred to a few thousand tokens each — like
`astrojones-mem-ingest-wisely`), not raw file dumps:
- **tech_stack** — languages, frameworks, runtimes, notable dependencies;
- **suggested_commands** — how to build/test/lint/run (from `agent/tools/*`, health config, task runners);
- **conventions** — code style, commit format, naming, testing method the repo actually uses;
- **structure/entrypoints** — the important paths and where execution starts.
Curate: what genuinely earns graph residency, not boilerplate.

Write this same curated content to `CLAUDE.md` at the repo root — the job Claude Code's built-in
`/init` normally does — inside a `<!-- astrojones:onboard:begin -->` / `<!-- astrojones:onboard:end -->`
marker pair (this skill's own convention; distinct from `scaffold.py`'s `AGENTS.md`-specific
section markers — don't reuse those):
- **No `CLAUDE.md` exists** — create it with four sections inside the markers: `## Tech stack`,
  `## Commands (build/test/lint/run)`, `## Conventions`, `## Structure & entrypoints`.
- **`CLAUDE.md` exists with the markers** — replace only the content between them; leave
  everything before/after untouched.
- **`CLAUDE.md` exists without the markers** (pre-existing hand-written file) — append a new
  marked block at the end; never overwrite or delete existing content.
- **`CLAUDE.md` is a symlink** (e.g. to `AGENTS.md` — a common convention for sharing one file
  across tools) — do not write through it. Skip this step; the symlink's target already carries
  the repo's shared guidance, and writing through it would silently mutate that other file.
Never delete `CLAUDE.md` or anything outside the markers — same non-destructive spirit as the
note on prior manual onboarding below.

### 5. Cost gate — dry run
Ingest into **the project's dataset** — named after the project, defaulting to this repo's
directory name (e.g. `astrojones`) or the `[project-name]` argument if given. A dataset is
**per project, not per repo**: a project may span several repos, and they all share one dataset
(parity with `kolbe`). If the project already has a dataset, reuse it — don't create a second.
cognee creates the dataset on first write. This does **not** fragment recall: session-start
recall queries `dataset=None` (the user's default scope, `agent_hooks._recall_section`), which
spans every dataset — so a project dataset is found exactly like a shared one, while keeping each
project's graph cleanly separable and its ontology bound to its own data. The `repo:<name>` tag
in `node_set` keeps each contributing repo filterable within a multi-repo project dataset.

`mem_ingest(items, dataset="<project>", node_set=["project_docs", "repo:<repo>"], ontology_key=<key>, dry_run=true)`.
Read `estimated_tokens` and `estimated_cost_usd`. If it's high, curate harder rather than
accepting blindly.

### 6. Confirm the spend — `AskUserQuestion`
Show the estimate and ask the user to approve before spending. **Never blindly pass
`confirm=true`** to silence the gate. Under `--noninteractive`, an under-limit spend proceeds
automatically; an over-limit spend still stops rather than auto-confirming.

### 7. Ingest
`mem_ingest(items, dataset="<project>", node_set=["project_docs", "repo:<repo>"], ontology_key=<key>, confirm=<if needed>)`
with the **same** `ontology_key` from step 3 and the **same** project `dataset` from step 5. The harness handles the fresh-dataset cognify
race automatically. Extraction runs in the background — `mem_stats(dataset)` shows pipeline
state; give a batch minutes and don't re-submit (that duplicates).

### 8. Mark done — `repo_onboard_complete`
Call `repo_onboard_complete` with the project `dataset` and `ontology_key`. This sets **this
repo's** flag (pointing at the shared project dataset) so future sessions stop nudging. Onboard
once per project: when another repo joins an already-onboarded project, just point its flag at the
existing dataset (mark it complete) rather than re-pinning the ontology and re-ingesting.

### 9. Acceptance — three canary queries
Retrieval is the acceptance test, not the 200 response. Run three `mem_search` queries:
- **historical** — a "what is here / how does it work" orientation question;
- **relational** — a question whose answer must CONNECT two ingested items (GRAPH_COMPLETION);
- **onboarding** — the broad "what is <repo> and how is it structured?" (GRAPH_COMPLETION).
Pass: all three answer from the ingested content **and** the entity types collapse to the
pinned set. Partial (chunks present, relations missing): the vocabulary is noisy — run
`astrojones-graph-tune` and canary again.

**CLAUDE.md check** — unless `CLAUDE.md` was a symlink (skipped by design, see step 4), read the
file back and confirm the marker block is present, non-empty, and contains all four sections.
Not a `mem_search` — a direct file read.

## Note on prior manual onboarding

The repo may already carry `.serena/memories/*.md` from an earlier manual Serena onboarding.
This skill **may offer** to migrate that content into cognee via the `mem_*` tools (curate →
dry-run → confirm → ingest under the same `ontology_key`). It must **never** auto-delete disk
memories or graph data — cleanup is always an explicit, separate user decision.
