---
name: implement
description: Use when taking a task from spec to done in a repository that has the repo-agent-harness — a task file, an issue, or a clear inline description. Runs the repo's end-to-end pipeline — spec gate → plan → implement via parallel TDD agents (strict RED/GREEN/REFACTOR) → verify → ship — wired to the harness tools and bundled subagents.
argument-hint: <task-file-or-description>
---

# implement — Spec-Driven TDD Pipeline

Takes a task and runs it end-to-end with the harness:
**spec gate → plan → implement (TDD) → verify → ship.**

```
implement @tasks/42-feature.md
implement "Add a --json flag to the export command"
```

**Prerequisite:** the task must be specified enough to distill acceptance criteria.

Copy this checklist into your response and tick items as you go — it keeps the pipeline
honest across a long, multi-phase task:

```
Implement Progress:
- [ ] Phase 0  Branch chosen; context + blast radius scouted
- [ ] Phase 1  Spec distilled; confidence >=90%
- [ ] Phase 2  Plan with disjoint stream ownership written
- [ ] Phase 3  Streams implemented (failing test first) + reviewed
- [ ] Phase 4  Quality gate green; no regressions vs baseline
- [ ] Phase 5  Verified it actually works (where applicable)
- [ ] Phase 6  Shipped: atomic commits; CI green where it exists
```

---

## Methodology (front-loaded, non-negotiable)

Everything below operates under:

**strict DDD/TDD RED/GREEN/REFACTOR for DRY/KISS/YAGNI/SOLID, with defensive programming.**

- **DDD** — use the domain's ubiquitous language in test names, variables, and module
  boundaries. Match the nouns the codebase already uses.
- **TDD — the iron law:** *no production code without a failing test first.* Wrote code
  before the test? Delete it and start from the test.
  - **RED** — one minimal failing test. Run it. Confirm it fails for the *right* reason
    (behavior missing, not a typo/import error).
  - **GREEN** — minimal code to pass. Run it. Confirm pass + clean output (no new warnings).
  - **REFACTOR** — only while green. DRY (extract at 3+ occurrences), KISS, YAGNI, SOLID
    checkpoint after each refactor.
- **Defensive programming** — validate at boundaries, fail loud and early, guard the
  empty/None/duplicate case. Never trust unvalidated input; never silently swallow an error
  that should surface. Guard clauses over deep nesting.
- **Reuse before reinvent** — before writing anything new, search for an existing utility,
  helper, fixture, or pattern (`repo_search_text`, Serena). The best code added here looks
  like it was already here.

---

## Phase 0 — Branch & Context

### 0.0 Branch strategy (ask)

Ask via `AskUserQuestion` whether to run in an isolated git worktree (recommended for
multi-file work) or on the current branch. If worktree: `EnterWorktree` with a name derived
from the task (`feat-json-export`); the new directory is the working root for every later
phase. Resolve all paths from the working root — never hardcode absolutes.

### 0.1 Activate context

1. Read the task file / parse the description / fetch the issue.
2. Orient with `repo_context_overview` (languages, entrypoints, important paths).
3. Activate Serena for symbol work; fall back to `repo_search_*` + `repo_read_range` (and
   Glob/Read) if Serena is unavailable.
4. Read the repo's `CLAUDE.md`/`AGENTS.md` for conventions.

### 0.2 Blast radius

Dispatch the **`explorer`** subagent in scout/breadth mode to map the relevant files and
symbols (a focused reading list for the blast radius), and run `repo_impact_file` on the
likely targets. Know what you're about to touch — especially for auth, schema/migration, or
exported-symbol changes.

---

## Phase 1 — Spec & Confidence Gate

**No code is written in this phase.**

### 1.1 Distill the spec

Write a concise spec: domain language, testable acceptance criteria, affected layers
(interface / logic / tests), and any migration or config implications.

### 1.2 Confidence check (≥90% to proceed)

Score yourself against this rubric:
1. **No duplicate** — searched for existing functionality; not reinventing.
2. **Architecture compliance** — uses the repo's existing layering, patterns, and stack; no
   unnecessary new dependencies.
3. **Docs verified** — library/API specifics confirmed against real docs, not memory.
4. **Root cause** (for bugfixes) — the actual cause is identified, not a symptom.

**Gate:** ≥90% → proceed and run autonomously to completion. 70–89% → ask 1–2 targeted
questions (`AskUserQuestion`) — or, if the `superpowers` plugin is available, invoke
`superpowers:brainstorming` — then re-score. <70% → stop and request context.

---

## Phase 2 — Plan

Decompose the work into **streams** with explicit, disjoint file ownership. Use
`sequential-thinking` if available; otherwise decompose directly.

- Streams with **disjoint file sets** run in parallel; streams that share files run
  sequentially.
- Each stream is owned by one **`implementer`** subagent and carries its own TDD breakdown.
- Schema/migration or auth/security changes: plan them explicitly and confirm the approach
  before editing.

Produce a short plan document:

```markdown
## Implementation Plan: [Title]

### Streams
| Stream | Owner       | Responsibility | Files            | Runs        |
|--------|-------------|----------------|------------------|-------------|
| 1      | implementer | ...            | path/a, path/b   | parallel    |
| 2      | implementer | ...            | path/c           | after 1     |

### Per-Stream TDD Breakdown
#### Stream 1
1. RED: failing test for [behavior]
2. GREEN: minimal implementation
3. REFACTOR: SOLID + defensive-guard checkpoint

### Acceptance Criteria
- [ ] ...
```

---

## Phase 3 — Implement (parallel TDD subagents)

**The main session coordinates; the `implementer` subagents write the code.**

### 3.0 Baseline

Run `repo_verify_changed` (or `agent/tools/test-changed`) to record a green baseline for
regression detection.

### 3.1 Spawn implementers per stream

Dispatch one **`implementer`** subagent per stream — disjoint streams in parallel (multiple
Task calls in one message), overlapping streams sequentially. Give each agent: its
assignment, its file list (do not edit outside it), the acceptance criteria, and the
methodology. The implementer follows the TDD iron law itself.

### 3.2 Two-stage review per stream

After an implementer reports done, before accepting the stream:
1. **Spec-compliance** — does it meet the acceptance criteria, nothing missing or out of
   scope? Gaps → implementer fixes → re-review.
2. **Code-quality** — dispatch the **`reviewer`** subagent over `repo_diff_current`:
   defensive guards, DRY/SOLID, conventions, no leaked secrets. Issues → fix → re-review.

Only when both are clean is the stream done. If an agent fails, retry once, then fall back
to direct tools.

---

## Phase 4 — Quality Gate

1. Run the full narrow verification with the **`test-runner`** subagent (`repo_verify_changed`).
2. **Regression check** — compare to the Phase 3.0 baseline: no new failures outside the
   newly-written tests; test count did not shrink unexpectedly.
3. Review `repo_diff_current` once more for scope creep — revert anything outside the spec.
4. On cleanup opportunities (duplication, over-engineering), tighten now while tests are green.
5. On failure: fix, max 2 rounds. Still failing → escalate to deeper root-cause analysis
   (the `superpowers:systematic-debugging` skill if available; otherwise form one hypothesis
   at a time, add a failing test that isolates it, then fix).

---

## Phase 5 — Verify it works (when applicable)

Tests passing ≠ feature works. If the repo has a way to exercise the change for real (a CLI
to run, a server to hit, a browser flow), do it and confirm the visible outcome — this is
the verification-before-completion gate. A real bug → loop back to Phase 3 TDD (failing test
first, then fix). Skip only when there is no user-facing behavior to exercise.

---

## Phase 6 — Ship

Commit the work as atomic conventional commits grouped by concern
(`feat:`/`fix:`/`test:`/`refactor:`/`chore:`), updating the task file / issue. Never mention
AI/Claude in commit messages. Push and confirm CI is green if the repo has CI; **done = CI
green** where CI exists.

If a worktree was created in Phase 0, ask whether to keep it for follow-up or remove it
(`ExitWorktree`).

---

## Completion report

```
## Implementation Complete
### Methodology: strict DDD/TDD RED/GREEN/REFACTOR, defensive programming
### Streams: [owner] — [files]
### TDD: [N] cycles, [M] tests added
### Verify: [how it was exercised — pass]
### Commits: [hash msg ...]
### Tests: [passed (baseline)] · regressions: none
### CI: [green/red — run URL, if any]
### Follow-ups: [...]
```

---

## Critical rules

- **Ask worktree vs current branch** (Phase 0.0); resolve all paths from the working root.
- **Confidence ≥90% before any code** — the spec gate is the entry point.
- **Iron law: failing test first** — code-before-test gets deleted.
- **Defensive programming** — validate boundaries, guard None/empty/duplicate, fail loud.
- **Reuse before reinvent** — search for existing patterns/fixtures first.
- **Real subagents, not the main session** — spawn `implementer`s; parallel only when file
  sets are disjoint. Describing agents in a table but never spawning them is the #1 failure.
- **Verify it works before claiming done** (Phase 5) — "tests pass" ≠ "it works".
- **Never declare done before CI is green** (where CI exists). Never mention AI/Claude in commits.
- On ambiguity after the gate: make a reasonable choice and note it — don't stop.

## Anti-patterns

- Doing all the work in the main session instead of spawning `implementer`s.
- Skipping the spec/confidence gate and charging into code.
- Writing implementation before a failing test exists.
- Running streams sequentially when their file sets don't overlap.
- Skipping the "verify it works" gate.
- Swallowing errors / trusting unvalidated input.
- Reinventing a utility or fixture that already exists.
- Changing global linter config instead of scoping the fix.
- Recursively reading the repo instead of using `repo_read_range` + Serena.
