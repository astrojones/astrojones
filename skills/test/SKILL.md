---
name: test
description: Use when writing, repairing, or running tests in a repository that has the repo-agent-harness. Guides narrow, targeted testing and disciplined failure triage.
---

# Test workflow

1. **Identify the likely test target** for the change (the harness maps changed source files to tests).
2. **Run the narrowest test** via `repo_verify_changed` (or `agent/tools/test-changed`) — not the full suite.
3. **On failure, classify** before fixing:
   - test bug (wrong assertion/setup),
   - setup/environment bug,
   - genuine product bug.
4. **Fix at the right layer** — don't paper over a product bug by weakening the test.
5. **Never update snapshots without a concrete reason.**
