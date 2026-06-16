---
name: plan
description: Use when planning a task in a repository that has the repo-agent-harness — turning a task file, an issue, or an inline description into an approved implementation plan before any code is written. Runs the harness-native plan flow (the `explorer` and read-only `architect` subagents, Serena-powered) in place of the built-in `Explore`/`Plan` agents, and owns the plan-mode gate end to end. Invoked as `/astrojones:plan`.
argument-hint: <task-file-or-description>
---

# plan — harness-native plan mode

Produces an approved implementation plan using the harness's read-only subagents instead of the
built-in `Explore`/`Plan` agents. The agents navigate by **symbol** (Serena) rather than reading
whole files, and they are **dispatched workers** — they return findings/design and never touch the
plan-mode gate. **This skill is the only caller of `EnterPlanMode` / `ExitPlanMode`, and the only
writer of the plan file.**

```
/astrojones:plan @tasks/42-feature.md
/astrojones:plan "Add a --json flag to the export command"
```

## Contract (must hold)

- **Only this skill** calls `EnterPlanMode` and `ExitPlanMode`, and **only this skill** writes the
  plan file. The subagents do neither.
- `explorer` and `architect` are **read-only dispatched workers**: they return their
  findings/design to this orchestrator and never enter/exit plan mode or mutate anything.
- No production code is written in plan mode — the plan file under `~/.claude/plans/` is the single
  write the gate permits.

## Flow

### 0. Enter plan mode
Call `EnterPlanMode`. Parse the task (read the task file / fetch the issue / take the inline
description). Orient once with `repo_context_overview` (languages, entrypoints, important paths).

### 1. Explore (breadth, then depth) — dispatch `explorer`
Dispatch the **`explorer`** subagent to map the blast radius:
- **Scout** for an unfamiliar region → a focused reading list of the relevant files and symbols.
- **Explore (depth)** for "how does X work / what breaks if I change it" → a cited data-flow answer.
Run `repo_impact_file` on the likely targets yourself for a quick blast-radius read. The explorer
returns conclusions with `path:line` citations — it does not dump files into this context.

### 2. Design — dispatch `architect`
Dispatch the read-only **`architect`** subagent with the task and the explorer's findings. It
returns a cited plan: critical files & symbols, the contract/data shape (for a product surface),
the sequenced steps or disjoint streams, the architectural trade-offs, and the blast radius. The
architect writes nothing and never calls `ExitPlanMode`.

### 3. Resolve forks — `AskUserQuestion`
If the architect surfaced genuine forks (an either/or the task doesn't settle, a trade-off only the
user can pick), ask them with `AskUserQuestion` and fold the answers into the plan. Do not ask about
choices with an obvious default — decide those and note them.

### 4. Write the plan file
Write the finalized plan to **`~/.claude/plans/<slug>.md`** (mirrors the built-in plan directory;
`<slug>` is a kebab-case summary of the task). This durable file is the source of truth — it is the
one write plan mode allows. Do not also rely on `ExitPlanMode` to persist the plan; the file is the
artifact, `ExitPlanMode` is only the approval gate (next step).

### 5. Present for approval — `ExitPlanMode`
Call `ExitPlanMode` with the plan for the user to approve. Execution begins only after they accept;
hand the streams to the `implementer` agent (or the `feature` / `bugfix` / `refactor` skills).

## Fallback (harness absent)

This skill and its subagents ship in the astrojones plugin. Where the harness is **not** present,
the skill simply isn't invoked and the built-in `Explore` / `Plan` agents run the planning flow by
default — that is the intended degradation; there is nothing to switch off. If the harness MCP tools
or the `explorer` / `architect` subagents are unavailable mid-flow (e.g. Serena failed to launch),
fall back to the built-in `Explore` / `Plan` subagents for that step rather than blocking.

## Critical rules

- **Own the gate.** Only this skill calls `EnterPlanMode` / `ExitPlanMode` and writes the plan file.
- **Agents are read-only workers** — dispatch them, use their returned conclusions, never let them
  drive the session or mutate code.
- **Plan only.** No production code in this skill; the plan file is the single permitted write.
- **Citations carry through** — the plan names files and symbols as `path:line`, so execution is
  directly actionable.
