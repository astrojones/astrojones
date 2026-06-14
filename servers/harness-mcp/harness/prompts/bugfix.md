# Bugfix workflow

1. **Reproduce / locate** the failure — the error message, failing test, or stack trace.
2. **Find the relevant code**
   - Symbols: Serena `find_symbol` / `find_referencing_symbols`.
   - Files: `repo_context_relevant_files` (heuristic shortlist).
3. **Read only what you need** with `repo_read_range` — never dump whole files.
4. **Assess blast radius** — if the fix touches a shared symbol, run `repo_impact_file` first.
5. **Fix the root cause** with the smallest possible edit. No unrelated cleanup.
6. **Verify** with `repo_verify_changed`; review `repo_diff_current` for scope creep.
7. **Summarize** the root cause, the fix, and any residual risk.

Never recursively read the repo. Prefer the harness tools over raw shell.
