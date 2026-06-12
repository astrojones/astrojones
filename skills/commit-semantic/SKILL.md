---
name: commit-semantic
description: Creates semantic commits from working tree changes, grouping related changes chronologically and semantically. Plans commit groups with the main agent, then fans out to a swarm of Haiku subagents that draft commit messages in parallel before the main agent commits sequentially. Use when the user asks to clean up git status, create semantic commits, or organize changes into logical commits.
---

# Commit Semantic

Create semantic, chronological commits from working tree changes.
Plans commit groups first, then fans out to Haiku subagents for parallel message-drafting,
and commits sequentially in the correct order.

## Phases

```
Phase 1 — Plan    (main agent)   Analyze tree, group changes, determine order
Phase 2 — Draft   (Haiku swarm)  One subagent per group drafts a precise commit message
Phase 3 — Commit  (main agent)   Execute commits sequentially, show log
```

---

## Phase 1 — Plan

**1.1 Read the working tree**

Run these in parallel:
- `git status --short` — list of changed files and their state
- `git diff HEAD` — full diff of all staged + unstaged changes

**1.2 Group changes semantically**

Assign every changed file to exactly one group. Each group becomes one commit.
Guiding rules for grouping:
- Renames / moves together
- A feature and its tests together
- Documentation together (unless it's a standalone docs-only commit)
- Config changes together
- Infra / build changes together (Dockerfile, CI, pyproject, lockfile)
- Refactoring separate from behaviour changes

**1.3 Order groups chronologically**

Determine the dependency order so no commit references something that doesn't exist yet:
- Foundation before dependents (config before code that reads it)
- Renames before new references to the renamed path
- Infra before application code

**1.4 Produce a commit plan**

Build an explicit plan — a numbered list in commit order:

```
Commit Plan:
1. chore — files: [pyproject.toml, uv.lock]
2. feat  — files: [src/foo/bar.py, tests/test_bar.py]
3. docs  — files: [README.md]
```

Show the plan to the user before proceeding. If anything is wrong, stop here.

---

## Phase 2 — Draft (parallel Haiku swarm)

For each group in the plan, spawn one Agent subagent with `model: haiku`.
**All subagents run in parallel** — launch them in a single batch.

Each subagent receives:
- The file list for its group
- The diff for those files only (`git diff HEAD -- <file1> <file2> ...`)
- Its position in the commit order

Each subagent's prompt:

```
You are drafting a single git commit message.

Files in this commit:
<file list>

Diff:
<diff output>

Rules:
- Use Conventional Commits format: <type>(<optional scope>): <short summary>
- Types: feat, fix, refactor, docs, chore, test, perf, ci, build
- Summary: imperative mood, ≤72 chars, no trailing period
- Body (optional): add only if the WHY is non-obvious; plain prose, 72-char wrap
- Return ONLY the commit message (subject + optional blank line + body). No preamble.
```

Collect each subagent's returned string as `messages[i]`.

---

## Phase 3 — Commit (sequential)

For each group `i` in chronological order (1 → N):

1. Stage the files:
   ```bash
   git add -- <file1> <file2> ...
   ```
2. Commit with the drafted message:
   ```bash
   git commit -m "$(cat <<'EOF'
   <messages[i]>
   EOF
   )"
   ```
3. Capture and display the commit hash + subject.

**If the user requested push**, run `git push` after all commits.

**Show the final log** covering exactly the N commits just created:
```bash
git log --oneline -N
```

---

## Fallback (no subagent support)

If the Agent tool is unavailable, skip Phase 2 and draft messages inline during
Phase 3. The commit order and grouping rules still apply.
