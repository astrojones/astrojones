# Commit workflow (semantic commits)

Group working-tree changes into atomic, semantic commits. Each commit must
contain a single logical change that could be reverted independently.

1. **Survey the diff** — `git status` and `git diff` (unstaged) plus `git diff --staged`.
2. **Group related changes** by concern, not by chronology. A test + the code it
   exercises are one commit, not two.
3. **Order the commits** so each one is a green state. The repo must build and
   pass tests after each commit lands.
4. **Use a conventional commit prefix** in the subject line:
   - `feat:` — new user-visible behavior.
   - `fix:` — bug fix.
   - `refactor:` — behavior-preserving code change.
   - `test:` — only adding/fixing tests.
   - `docs:` — only documentation.
   - `chore:` — tooling, deps, housekeeping; no behavior change.
   - `perf:` — performance improvement.
   - `!` suffix on a type (e.g. `feat!:`) flags a breaking change.
5. **Subject line: imperative, lowercase, no trailing period, ≤72 chars.**
   Subject describes what the commit does, not what it did.
6. **Body** (optional) — explain *why*, not *what*. Reference the issue or
   design doc if the change is non-obvious.
7. **Never** mention the AI, the model, or the assistant in commit messages.
8. **No secrets, no credentials, no `.env` contents** in any commit.
9. **Stage precisely** — `git add <path>` (or `-p` for partial). Verify with
   `git diff --staged` before committing.
10. **Inspect before committing** — `git status` and `git log --oneline -5` to
    confirm the staged state and the commit style match the repo's history.

## Failure modes to avoid

- "WIP" commits with debug prints or commented-out code.
- One mega-commit for a multi-step feature.
- Mixing formatting fixes with behavior changes.
- Commits that don't build (test, lint, type-check) on their own.
- Subject lines that start with the type and then describe the previous state
  ("fix: was broken when...") instead of the fix.
