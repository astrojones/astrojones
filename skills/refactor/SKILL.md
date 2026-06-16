---
name: refactor
description: Use when restructuring or cleaning up code without changing behavior in a repository that has the repo-agent-harness. Enforces a behavior-preserving, scope-limited refactor with impact analysis and verification.
---

# Refactor workflow

1. **State the target** — the behavior-preserving change you intend (rename, extract, move, simplify).
2. **Check impact** — run `repo_impact_file` on the targets; note dependents and test coverage. If the blast radius is unfamiliar, dispatch the **`explorer`** subagent to map the dependents (by symbol) before you touch them.
3. **Make the mechanical change** only. Resist unrelated cleanup ("while I'm here" is how refactors break).
4. **Verify** with `repo_verify_changed` — behavior must be unchanged.
5. **Review `repo_diff_current`** specifically for scope creep; revert anything outside the stated target.

If impact is "high" (auth/payments/migrations/schema), confirm the plan before editing.
