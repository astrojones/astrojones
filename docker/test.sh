#!/usr/bin/env bash
# Clean-room smoke test for the astrojones plugin. No cognee creds, no host config.
#
# Layer 1 (deterministic, free):
#   1. PreToolUse hook denies a dangerous command (proves: python3 present, shim resolves,
#      harness venv warm, default shell policy loaded — the safety guard actually fires).
#   2. MCP server boots over stdio and registers repo_context_overview (proves: the bundled
#      server starts and exposes its tools in a vanilla env).
#   3. The plugin's own pytest suite passes (regression signal — the harness code itself works).
# Layer 2 (optional, needs CLAUDE_CODE_OAUTH_TOKEN):
#   4. Real `claude -p` end-to-end: claude calls repo_context_overview and reports the repo's
#      languages (proves: Claude Code auto-connects the plugin's MCP server and the tool is
#      callable from the model).
# Layer 3 (optional, needs CLAUDE_CODE_OAUTH_TOKEN):
#   5. Scenario-driven `claude -p` runs (docker/e2e_hooks.sh) proving Claude Code's own
#      hook DISPATCH works — deny wins, state files written, additionalContext injected —
#      asserted machine-readably by docker/e2e_verify.py (never on model prose).
set -uo pipefail
# Host-safety: this ENTRYPOINT mutates git state in /workspace/repo; a host run once
# committed into the checked-out repo. Refuse outside a container.
if [ ! -f /.dockerenv ] && [ ! -f /run/.containerenv ] && [ "${ASTROJONES_TEST_IN_CONTAINER:-}" != "1" ]; then
  echo "docker/test.sh runs only inside the container — use docker/run.sh" >&2
  exit 1
fi
PLUGIN=/plugins/astrojones
HARNESS=$PLUGIN/servers/harness-mcp
PASS=0; FAIL=0
ok(){ echo "  PASS: $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

PY=$HARNESS/.venv/bin/python  # the warm uv-managed 3.13 — never call bare python3

echo "### environment"
node --version; claude --version; uv --version; git --version
"$PY" --version
[ -x "$PY" ] && ok "harness venv pre-built (warm)" || no "harness venv missing"
command -v python3 >/dev/null && ok "python3 on PATH (plugin hooks.json contract)" || no "python3 missing — plugin hooks CANNOT fire in this env"

echo; echo "### TEST 1: PreToolUse hook denies a dangerous command (deterministic)"
mkdir -p /workspace/repo && cd /workspace/repo || exit 1
git init -q 2>/dev/null; git config user.email t@t 2>/dev/null; git config user.name t 2>/dev/null
printf 'def greet(name):\n    return f"hi {name}"\n\nclass App:\n    def run(self):\n        return greet("world")\n' > app.py
git add -A 2>/dev/null && git commit -qm init 2>/dev/null
DEC=$(printf '%s' '{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/foo"}}' \
      | "$PY" "$PLUGIN/hooks/pre_tool_use.py")
echo "  hook decision: $DEC"
echo "$DEC" | grep -q '"permissionDecision":[[:space:]]*"deny"' && ok "hook denies rm -rf" || no "hook did not deny rm -rf"

echo; echo "### TEST 1b: all 4 hooks fire + return valid JSON + exit 0 (fail-open contract)"
# Each hook is invoked exactly as Claude Code does — `python3 $PLUGIN/hooks/<name>.py`
# with the event JSON on stdin — and must: exit 0, emit valid JSON, stay under the 10s
# budget. session_start specifically must fail open with no cognee creds: recall contributes
# nothing, leaving the deterministic onboarding nudge (fresh container = never onboarded).
# post_tool_use must emit its verify nudge. This is the regression check that the hook
# layer — which we put a lot of thought into — keeps working in a vanilla env.
fire_hook() {
  file="$1"; payload="$2"; expect="$3"
  OUT=$(printf '%s' "$payload" | "$PY" "$PLUGIN/hooks/$file" 2>/tmp/he); RC=$?
  if [ "$RC" -eq 0 ] && printf '%s' "$OUT" | "$PY" -c 'import sys,json; json.load(sys.stdin)' 2>/dev/null; then
    if [ -z "$expect" ] || echo "$OUT" | grep -q "$expect"; then
      ok "$file fires (exit 0, valid JSON${expect:+, match:$expect})"
    else
      no "$file fired but missing expected output (want: $expect, got: $OUT)"
    fi
  else
    no "$file failed (exit $RC, stderr: $(cat /tmp/he))"
  fi
}
cd "$PLUGIN" || exit 1
fire_hook session_start.py      '{"hook_event_name":"SessionStart","cwd":"'"$PLUGIN"'","source":"startup"}' 'onboarded into durable memory'
fire_hook pre_tool_use.py       '{"hook_event_name":"PreToolUse","tool_name":"Bash","tool_input":{"command":"ls"}}' '^{}$'
fire_hook post_tool_use.py      '{"hook_event_name":"PostToolUse","tool_name":"Edit","tool_input":{"file_path":"'"$PLUGIN"'/README.md","old_string":"a","new_string":"b"},"tool_response":{"success":true}}' 'repo_verify_changed'
fire_hook user_prompt_submit.py '{"hook_event_name":"UserPromptSubmit","prompt":"fix the bug in app.py"}' ''
cd /workspace/repo || exit 1

echo; echo "### TEST 2: MCP server boots and registers repo_context_overview"
if "$PY" "$PLUGIN/docker/mcp_probe.py" "$HARNESS"; then ok "MCP server lists repo_context_overview"; else no "MCP server/probe failed"; fi

echo; echo "### TEST 3: plugin's own pytest suite (regression signal)"
if ( cd "$HARNESS" && uv run pytest -q 2>&1 | tail -8 ); then ok "pytest suite ran"; else no "pytest suite errored"; fi

echo; echo "### TEST 4 (optional): claude -p end-to-end MCP call (needs CLAUDE_CODE_OAUTH_TOKEN)"
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  cd /workspace/repo || exit 1
  OUT=$(claude -p --plugin-dir "$PLUGIN" --permission-mode bypassPermissions \
        "Call the mcp__plugin_astrojones_repo-agent-harness__repo_context_overview tool with no arguments, then reply with EXACTLY one line: LANGUAGES=<the 'languages' array, comma-joined>." 2>&1) || true
  echo "  claude said: $(echo "$OUT" | tail -3)"
  echo "$OUT" | grep -qi 'python' && ok "e2e: claude called repo_context_overview" || no "e2e: no python in claude reply"
else
  echo "  skipped (set CLAUDE_CODE_OAUTH_TOKEN to run)"
fi

echo; echo "### TEST 5 (optional): claude -p hook-dispatch scenarios (needs CLAUDE_CODE_OAUTH_TOKEN)"
if [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}" ]; then
  if bash "$PLUGIN/docker/e2e_hooks.sh"; then ok "e2e hook scenarios"; else no "e2e hook scenarios"; fi
else
  echo "  skipped (set CLAUDE_CODE_OAUTH_TOKEN to run)"
fi

echo; echo "### RESULT: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]