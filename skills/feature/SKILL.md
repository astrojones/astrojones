---
name: feature
description: Use when adding a new feature or capability to a repository that has the repo-agent-harness. Guides a smallest-vertical-slice implementation modeled on existing patterns, with tests and targeted verification.
---

# Feature workflow

1. **Orient** — `repo_context_overview` for languages, entrypoints, and important paths.
2. **Find a comparable feature** already in the codebase (Serena + `repo_context_relevant_files`) and mirror its structure.
3. **Identify the files** involved: API/interface, domain/logic, and tests.
4. **Implement the smallest vertical slice** that delivers value end-to-end.
5. **Add or update tests** alongside the code.
6. **Verify** with `repo_verify_changed`; review `repo_diff_current`.

Prefer range reads and the harness tools; do not recursively read the repo.
