# Refactor workflow

1. **State the target** — the behavior-preserving change you intend (rename, extract, move, simplify).
2. **Check impact** — run `repo_impact_file` on the targets; note dependents and test coverage.
3. **Make the mechanical change** only. Resist unrelated cleanup ("while I'm here" is how refactors break).
4. **Verify** with `repo_verify_changed` — behavior must be unchanged.
5. **Review `repo_diff_current`** specifically for scope creep; revert anything outside the stated target.

If impact is "high" (auth/payments/migrations/schema), confirm the plan before editing.
