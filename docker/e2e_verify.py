#!/usr/bin/env python
"""Layer 3 verifier: machine-readable assertions for one `claude -p` hook scenario.

Called by e2e_hooks.sh once per scenario. Never asserts on model prose — evidence is:
  1. filesystem side effects under the scenario's REPO_AGENT_HARNESS_HOME (hard),
  2. hook lifecycle events in the saved `--output-format stream-json --verbose` stream,
  3. the session transcript JSONL under ~/.claude/projects/<slug>/<session_id>.jsonl.

Evidence shapes (verified by the Phase 0 probes, claude CLI 2.x, 2026-07):
  - stream-json emits system/hook_started + system/hook_response ONLY for SessionStart;
    the hook's full stdout (hookSpecificOutput JSON) is embedded in the event's "output".
  - A PreToolUse deny surfaces as a user-type tool_result with is_error=true and the
    harness reason text "Denied by policy (...)". No PreToolUse lifecycle event exists.
  - PostToolUse / UserPromptSubmit / Stop emit no stream lifecycle events at all; their
    injected context lands in the transcript as "hook_additional_context" attachments,
    and their durable evidence is the state files each hook writes.
Checks whose evidence source the probes confirmed are hard FAILs; the rest are WARNs
(schema drift in `claude@latest` must degrade loudly, not flakily).

Stdlib-only; run with the harness venv python.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import sqlite3
import sys
from pathlib import Path

# One scenario check = (kind, ok, label): kind "hard" fails the scenario, "soft" warns.
Check = tuple[str, bool, str]


def repo_hash(repo: str) -> str:
    """Mirror paths.repo_id(): sha256 of the repo realpath, first 12 hex chars."""
    return hashlib.sha256(os.path.realpath(repo).encode()).hexdigest()[:12]


def state_repo_dir(state: str, repo: str) -> Path:
    return Path(state) / "repos" / repo_hash(repo)


def _ingest(path: Path, lines: list[str]) -> str | None:
    """Append one JSONL file's lines (re-serialized when parseable, so key spacing is
    canonical and substring checks are stable); return the session_id if seen."""
    session_id = None
    try:
        raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        print(f"  WARN: cannot read {path}: {exc}")
        return None
    for raw in raw_lines:
        raw = raw.strip()
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except ValueError:
            lines.append(raw)  # tolerate non-JSON verbose noise
            continue
        lines.append(json.dumps(obj))
        if session_id is None and isinstance(obj, dict) and obj.get("subtype") == "init":
            session_id = obj.get("session_id")
    return session_id


def load_evidence(stream: str) -> list[str]:
    """Flat evidence list: the stream, plus the transcript found via its session_id."""
    lines: list[str] = []
    session_id = _ingest(Path(stream), lines)
    if not session_id:
        print("  WARN: no system/init session_id in stream — transcript evidence unavailable")
        return lines
    transcripts = sorted(Path.home().glob(f".claude/projects/*/{session_id}.jsonl"))
    if not transcripts:
        print(f"  WARN: no transcript found for session {session_id}")
    for t in transcripts:
        _ingest(t, lines)
    return lines


def has(lines: list[str], *substrings: str) -> bool:
    """True when one evidence line contains every given substring."""
    return any(all(s in line for s in substrings) for line in lines)


def _read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- scenario checks

def check_pretooluse_deny(lines: list[str], state_dir: Path, canary: str) -> list[Check]:
    return [
        ("hard", Path(canary).is_file(), f"canary {canary} survived the rm -rf attempt"),
        ("hard", has(lines, "Denied by policy"), "deny reason visible in stream/transcript"),
    ]


def check_posttooluse_edit(lines: list[str], state_dir: Path, canary: str) -> list[Check]:
    touched = _read_json(state_dir / "perception_touched.json")
    recorded = isinstance(touched, list) and any("notes.txt" in str(p) for p in touched)
    return [
        ("hard", recorded, f"perception_touched.json records notes.txt (got: {touched!r})"),
        # The verify nudge only fires when the perception daemon has no snapshot yet; the
        # daemon races the model's Write, so its absence is expected noise — soft. Match the
        # nudge sentence, not "repo_verify_changed": the tool name also appears in the
        # transcript's MCP tool listing, which would pass even with hooks disabled.
        ("soft", has(lines, "Before continuing, verify the change"), "PostToolUse verify nudge in evidence"),
    ]


def check_sessionstart_context(lines: list[str], state_dir: Path, canary: str) -> list[Check]:
    return [
        ("hard", has(lines, "hook_started", "SessionStart"), "SessionStart hook lifecycle event in stream"),
        ("hard", has(lines, "yet onboarded into durable memory"), "onboarding nudge injected as additionalContext"),
        ("soft", has(lines, "Repo symbol map"), "symbol map section injected"),
    ]


def check_stop_capture(lines: list[str], state_dir: Path, canary: str) -> list[Check]:
    db = state_dir / "brain" / "capture_queue.db"
    rows = 0
    if db.is_file():
        # Plain connect (not mode=ro URI): the run is over so nothing writes, and a
        # read-only open of a WAL database fails when a -wal file is left behind.
        try:
            with contextlib.closing(sqlite3.connect(db)) as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM capture_queue WHERE event = 'stop'"
                ).fetchone()[0]
        except sqlite3.Error as exc:
            print(f"  WARN: capture_queue.db unreadable: {exc}")
    return [
        ("hard", rows >= 1, f"capture_queue.db has a 'stop' row (found {rows})"),
        ("soft", has(lines, "hook_started", "Stop"), "Stop hook lifecycle event in stream"),
    ]


def check_userpromptsubmit_delta(lines: list[str], state_dir: Path, canary: str) -> list[Check]:
    return [
        ("hard", (state_dir / "perception_last_seen.json").is_file(), "perception_last_seen.json written"),
        ("soft", has(lines, "Repo perception update"), "perception delta digest in evidence"),
    ]


SCENARIOS = {
    "pretooluse-deny": check_pretooluse_deny,
    "posttooluse-edit": check_posttooluse_edit,
    "sessionstart-context": check_sessionstart_context,
    "stop-capture": check_stop_capture,
    "userpromptsubmit-delta": check_userpromptsubmit_delta,
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scenario", required=True, choices=sorted(SCENARIOS))
    ap.add_argument("--stream", required=True, help="saved stream-json output of the run")
    ap.add_argument("--state", required=True, help="the scenario's REPO_AGENT_HARNESS_HOME")
    ap.add_argument("--repo", required=True, help="repo the scenario ran in (hash source)")
    ap.add_argument("--canary", default="/tmp/e2e_probe/canary")
    ap.add_argument("--warn-only", action="store_true", help="downgrade hard fails to warns")
    args = ap.parse_args()

    lines = load_evidence(args.stream)
    state_dir = state_repo_dir(args.state, args.repo)
    checks = SCENARIOS[args.scenario](lines, state_dir, args.canary)

    failed = 0
    for kind, ok, label in checks:
        if ok:
            print(f"  PASS: [{args.scenario}] {label}")
        elif kind == "hard" and not args.warn_only:
            print(f"  FAIL: [{args.scenario}] {label}")
            failed += 1
        else:
            print(f"  WARN: [{args.scenario}] {label}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
