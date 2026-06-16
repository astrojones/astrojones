---
name: bugfix
description: Use when fixing a bug, diagnosing a failure, or chasing a stack trace in a repository that has the repo-agent-harness. Guides a safe, minimal-surface fix using Serena for navigation and the harness tools for context and verification.
---

# Bugfix workflow

1. **Reproduce / locate** the failure — the error message, failing test, or stack trace.
2. **Find the relevant code**
   - Unfamiliar or multi-file territory? Dispatch the **`explorer`** subagent (scout mode) to map it and hand back a focused reading list — it absorbs the file noise instead of flooding this session.
   - A known single symbol? Go direct: Serena `find_symbol` / `find_referencing_symbols`, with `repo_context_relevant_files` for a heuristic shortlist.
3. **Read only what you need** with `repo_read_range` — never dump whole files.
4. **Assess blast radius** — if the fix touches a shared symbol, run `repo_impact_file` first.
5. **Fix the root cause** with the smallest possible edit. No unrelated cleanup.
6. **Verify** with `repo_verify_changed`; review `repo_diff_current` for scope creep.
7. **Summarize** the root cause, the fix, and any residual risk.

Never recursively read the repo. Prefer the harness tools over raw shell.
