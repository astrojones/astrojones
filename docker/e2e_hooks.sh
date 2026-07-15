#!/usr/bin/env bash
# Layer 3: scenario-driven `claude -p` runs proving Claude Code's OWN hook dispatch works —
# hooks fire in a real session, a PreToolUse deny wins, additionalContext is injected.
# Layer 1b fires the hook scripts directly; this layer is the only place the dispatch
# path (hooks.json -> python3 shim -> harness) is exercised by the real CLI.
#
# Needs CLAUDE_CODE_OAUTH_TOKEN (or staged ~/.claude credentials). Called by test.sh TEST 5.
#
# Phase 0 probe outcomes (2026-07-15, claude CLI 2.x via @latest, node:24-slim image):
#   P0: stream-json --verbose emits hook_started/hook_response ONLY for SessionStart (full
#       hook stdout embedded in .output). PostToolUse/UserPromptSubmit/Stop emit no stream
#       lifecycle events — their evidence is state files + transcript
#       hook_additional_context attachments. e2e_verify.py encodes these shapes.
#   P1: the PreToolUse deny WINS under --permission-mode bypassPermissions (canary
#       survived; deny surfaces as a tool_result "Denied by policy (...)").
#   P2: --model claude-haiku-4-5 works under CLAUDE_CODE_OAUTH_TOKEN.
set -uo pipefail
PLUGIN=${CLAUDE_PLUGIN_ROOT:-/plugins/astrojones}
HARNESS=$PLUGIN/servers/harness-mcp
PY=$HARNESS/.venv/bin/python
REPO=${E2E_REPO:-/workspace/repo}
E2E_ROOT=/tmp/e2e
MODEL=${E2E_MODEL:-claude-haiku-4-5}
CANARY_DIR=/tmp/e2e_probe
PASS=0; FAIL=0
ok(){ echo "  PASS: $1"; PASS=$((PASS+1)); }
no(){ echo "  FAIL: $1"; FAIL=$((FAIL+1)); }

# Repo fixture — idempotent with test.sh TEST 1, self-sufficient when run standalone.
mkdir -p "$REPO"
( cd "$REPO" || exit 1
  git init -q 2>/dev/null; git config user.email t@t 2>/dev/null; git config user.name t 2>/dev/null
  [ -f app.py ] || printf 'def greet(name):\n    return f"hi {name}"\n\nclass App:\n    def run(self):\n        return greet("world")\n' > app.py
  git add -A 2>/dev/null && git commit -qm init 2>/dev/null )

# Reproduce paths.repo_id() so pre-seeds land where the hooks will look.
HASH=$("$PY" -c 'import hashlib,os,sys;print(hashlib.sha256(os.path.realpath(sys.argv[1]).encode()).hexdigest()[:12])' "$REPO")

# run_scenario NAME MAX_TURNS VERIFY_FLAGS PROMPT [ENV_K=V ...]
#   Fresh state home per scenario; stream + stderr saved under /tmp/e2e/<name>/.
#   VERIFY_FLAGS is passed to e2e_verify.py ("" for none, e.g. "--warn-only" for S5).
run_scenario() {
  local name="$1" max_turns="$2" verify_flags="$3" prompt="$4"; shift 4
  local dir="$E2E_ROOT/$name" state="$E2E_ROOT/$name/state"
  local run_rc=0 verify_rc=0
  mkdir -p "$state"
  ( cd "$REPO" && env REPO_AGENT_HARNESS_HOME="$state" "$@" \
      timeout 180 claude -p --plugin-dir "$PLUGIN" --permission-mode bypassPermissions \
      --output-format stream-json --verbose --max-turns "$max_turns" --model "$MODEL" \
      "$prompt" > "$dir/stream.jsonl" 2> "$dir/stderr.log" ) || run_rc=$?
  [ "$run_rc" -ne 0 ] && echo "  note: claude exited $run_rc for $name (verifier decides)"
  # verify_flags is deliberately word-split
  # shellcheck disable=SC2086
  "$PY" "$PLUGIN/docker/e2e_verify.py" --scenario "$name" --stream "$dir/stream.jsonl" \
      --state "$state" --repo "$REPO" --canary "$CANARY_DIR/canary" $verify_flags || verify_rc=$?
  if [ "$verify_rc" -eq 0 ]; then
    ok "scenario $name"
  else
    no "scenario $name"
    echo "  --- $name stderr tail ---"; tail -5 "$dir/stderr.log" | sed 's/^/  | /'
    echo "  --- $name stream tail ---"; tail -3 "$dir/stream.jsonl" | cut -c1-300 | sed 's/^/  | /'
  fi
}

rm -rf "$E2E_ROOT"

echo "-- S1: pretooluse-deny (deny wins even under bypassPermissions)"
rm -rf "$CANARY_DIR"; mkdir -p "$CANARY_DIR"; touch "$CANARY_DIR/canary"
run_scenario pretooluse-deny 3 "" \
  "Run exactly this bash command and nothing else: rm -rf $CANARY_DIR. Then reply with exactly: DONE"

echo "-- S2: posttooluse-edit (touched-path recording after a real Write)"
run_scenario posttooluse-edit 3 "" \
  "Use the Write tool to create notes.txt containing hello. Then reply with exactly: DONE" \
  REPO_AGENT_HARNESS_NO_SERENA_GATE=1

echo "-- S3: sessionstart-context (onboarding nudge injected at session start)"
run_scenario sessionstart-context 1 "" "Reply with exactly: OK"

echo "-- S4: stop-capture (Stop hook enqueues into the local capture queue)"
# COGNEE_BASE_URL only arms the enqueue path — port 9 is unreachable by design; the
# 1s recall timeout keeps session_start's (fail-open) recall attempt cheap.
run_scenario stop-capture 1 "" "Reply with exactly: OK" \
  COGNEE_BASE_URL=http://127.0.0.1:9 REPO_AGENT_HARNESS_RECALL_TIMEOUT_S=1

echo "-- S5: userpromptsubmit-delta (perception digest from a pre-seeded failing snapshot)"
# The last-seen marker is written whenever a perception snapshot exists at prompt time —
# the seed guarantees that, so the hard check is deterministic; only the digest content
# races the daemon and that check is soft in the verifier.
S5_STATE=$E2E_ROOT/userpromptsubmit-delta/state
mkdir -p "$S5_STATE/repos/$HASH"
printf '%s' '{"verdicts":[{"id":"tests","ok":false,"summary":"seeded failing check"}],"git":{"branch":"main","head":"deadbeef"}}' \
  > "$S5_STATE/repos/$HASH/perception.json"
run_scenario userpromptsubmit-delta 1 "" "Reply with exactly: OK"

echo "-- e2e result: $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
