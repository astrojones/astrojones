# Feature workflow

1. **Orient** — `repo_context_overview` for languages, entrypoints, and important paths.
2. **Find a comparable feature** already in the codebase and mirror its structure. In an unfamiliar area, dispatch the **`explorer`** subagent (scout mode) to locate it and return a reading list; in a familiar one, go direct with Serena + `repo_context_relevant_files`.
3. **Identify the files** involved: API/interface, domain/logic, and tests.
4. **Implement the smallest vertical slice** that delivers value end-to-end.
5. **Add or update tests** alongside the code.
6. **Verify** with `repo_verify_changed`; review `repo_diff_current`.

Prefer range reads and the harness tools; do not recursively read the repo.
