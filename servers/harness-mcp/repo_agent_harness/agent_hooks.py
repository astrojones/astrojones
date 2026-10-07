"""Claude Code hook handlers, exposed via ``repo-agent-harness hook <event>``.

Pure functions: take the hook event payload, return the hook JSON response
(empty dict = allow / no output). The CLI wrapper (``repo-agent-harness hook``)
or the lightweight ``main`` below — invoked as ``python -m
repo_agent_harness.agent_hooks <event>`` by the plugin hook to skip the heavy
CLI import — owns stdin/stdout and fail-open behavior, so a hook problem never
blocks legitimate work.
"""

from __future__ import annotations

import json
import sys
import time
from contextlib import suppress
from pathlib import Path

from repo_agent_harness import git, paths, policies, secrets

_GUARDED_FILE_TOOLS = {"Read", "Edit", "Write", "NotebookEdit"}
_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_VERIFY_NUDGE = (
    "A file was modified. Before continuing, verify the change: run repo_verify_changed "
    "(or agent/tools/safe-diff then agent/tools/test-changed) to check only what changed."
)


def _deny(reason: str) -> dict:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def pre_tool_use(data: dict, root: str | None = None) -> dict:
    """Deny dangerous shell commands and secret-path reads via repo policy."""
    tool = data.get("tool_name", "")
    tin = data.get("tool_input") or {}
    repo = root or git.repo_root()
    base = repo or str(Path.cwd())

    if tool == "Bash":
        cmd = tin.get("command", "")
        if cmd:
            check = policies.check_command(cmd, base)
            if not check.allowed:
                return _deny(check.reason)

    elif tool in _GUARDED_FILE_TOOLS:
        path = tin.get("file_path") or tin.get("path") or tin.get("notebook_path") or ""
        if path:
            cfg = secrets.load(base)
            try:
                rel = str(Path(path).resolve().relative_to(Path(base).resolve()))
            except ValueError:
                rel = path
            if secrets.is_secret_path(rel, cfg):
                return _deny(f"Accessing a secret path ('{rel}') is blocked by policy.")

    return {}


def antigravity_pre_tool_use(data: dict, root: str | None = None) -> dict:
    """Deny dangerous shell commands and secret-path reads for Antigravity."""
    tc = data.get("toolCall") or {}
    tool = tc.get("name", "")
    targs = tc.get("args") or {}
    repo = root or (data.get("workspacePaths") or [None])[0] or git.repo_root()
    base = repo or str(Path.cwd())

    if tool == "run_command":
        cmd = targs.get("CommandLine", "")
        if cmd:
            check = policies.check_command(cmd, base)
            if not check.allowed:
                return {"decision": "deny", "reason": check.reason}
            if check.requires_confirmation:
                return {"decision": "ask", "reason": check.reason}
            return {"decision": "allow"}

    elif tool in {"view_file", "write_to_file", "replace_file_content"}:
        path = targs.get("AbsolutePath") or targs.get("TargetFile") or ""
        if path:
            cfg = secrets.load(base)
            try:
                rel = str(Path(path).resolve().relative_to(Path(base).resolve()))
            except ValueError:
                rel = path
            if secrets.is_secret_path(rel, cfg):
                return {"decision": "deny", "reason": f"Accessing a secret path ('{rel}') is blocked by policy."}
            return {"decision": "allow"}

    return {}


def antigravity_post_tool_use(data: dict, root: str | None = None) -> dict:
    """PostToolUse hook for Google Antigravity: record touched files for perception."""
    tc = data.get("toolCall") or {}
    tool = tc.get("name", "")
    targs = tc.get("args") or {}
    repo = root or (data.get("workspacePaths") or [None])[0] or git.repo_root()

    if tool in {"write_to_file", "replace_file_content"}:
        path = targs.get("TargetFile") or ""
        if path and repo:
            _record_touched(repo, path)

    return {}


def _read_json(path: Path) -> dict | list | None:
    """Best-effort JSON read; None when the file is missing or unparseable (fail-open)."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _write_json(path: Path, obj: dict | list) -> None:
    """Best-effort JSON write (the parent dir is created by paths.repo_state_dir)."""
    with suppress(OSError):
        path.write_text(json.dumps(obj), encoding="utf-8")


def _record_touched(repo: str, path: str) -> None:
    """Append the agent-edited path to the per-repo touched-set (external-vs-agent attribution)."""
    try:
        rel = str(Path(path).resolve().relative_to(Path(repo).resolve()))
    except (ValueError, OSError):
        rel = path
    target = paths.perception_touched_file(repo)
    existing = _read_json(target)
    touched = existing if isinstance(existing, list) else []
    if rel not in touched:
        touched.append(rel)
        _write_json(target, touched)


def _perception_deltas(current: dict, last: dict | None) -> list[str]:
    """Lines describing what changed in perception since ``last`` (the snapshot last surfaced).

    With no prior marker (``last is None``) it reports the current hazards (failing checks,
    existing conflicts) as the initial perception; otherwise it reports only transitions
    (a check went red or recovered, a branch/HEAD switch, newly-appeared conflicts).
    """
    lines: list[str] = []
    last_verdicts = {v["id"]: v for v in (last or {}).get("verdicts", []) if isinstance(v, dict) and "id" in v}
    for v in current.get("verdicts", []):
        if not isinstance(v, dict) or "id" not in v:
            continue
        ok, prev = v.get("ok"), last_verdicts.get(v["id"], {}).get("ok")
        if ok is False and prev is not False:
            lines.append(f"{v['id']}: now FAILING — {str(v.get('summary', '')).strip()}".rstrip(" —"))
        elif ok is True and prev is False:
            lines.append(f"{v['id']}: recovered (passing again)")

    git_now, git_last = current.get("git") or {}, (last or {}).get("git") or {}
    b_now, b_last = git_now.get("branch", ""), git_last.get("branch", "")
    h_now, h_last = git_now.get("head", ""), git_last.get("head", "")
    if last is not None and b_now and b_last and b_now != b_last:
        lines.append(f"git: branch switched {b_last} -> {b_now} (possibly by another process)")
    elif last is not None and b_now == b_last and h_now and h_last and h_now != h_last:
        lines.append(f"git: HEAD moved {h_last} -> {h_now}")
    new_conflicts = sorted(set(git_now.get("conflicted") or []) - set(git_last.get("conflicted") or []))
    if new_conflicts:
        lines.append(f"git: merge conflicts in {', '.join(new_conflicts)}")
    return lines


def post_tool_use(data: dict, root: str | None = None) -> dict:
    """After an edit/write: record the touched path and surface any current check regression.

    When the perception daemon has a snapshot, this stays quiet on green (the harness is already
    re-running checks for you) and warns only when a check is red. With no snapshot yet (e.g. a
    non-MCP client with no running daemon) it falls back to the static verify nudge.
    """
    if data.get("tool_name", "") not in _EDIT_TOOLS:
        return {}
    repo = root or git.repo_root()
    tin = data.get("tool_input") or {}
    path = tin.get("file_path") or tin.get("path") or tin.get("notebook_path") or ""
    if repo and path:
        _record_touched(repo, path)
    snapshot = _read_json(paths.perception_file(repo)) if repo else None
    if not isinstance(snapshot, dict):
        return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": _VERIFY_NUDGE}}
    red = [v for v in snapshot.get("verdicts", []) if isinstance(v, dict) and v.get("ok") is False]
    if not red:
        return {}
    note = "Heads up — background checks currently failing: " + "; ".join(
        f"{v['id']} ({str(v.get('summary', '')).strip()})" for v in red
    )
    return {"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": note[:9000]}}


def user_prompt_submit(data: dict, root: str | None = None) -> dict:
    """Once per turn, inject a digest of perception changes since the last turn (deltas only).

    Reads the daemon's snapshot and the last-seen marker, emits only what changed (a check went
    red/recovered, an external branch/HEAD switch, new conflicts), then advances the marker so a
    standing failure is reported once, not re-nagged. Silent when nothing changed or no snapshot.
    """
    _ = data
    repo = root or git.repo_root()
    if not repo:
        return {}
    current = _read_json(paths.perception_file(repo))
    if not isinstance(current, dict):
        return {}
    last = _read_json(paths.perception_last_seen_file(repo))
    lines = _perception_deltas(current, last if isinstance(last, dict) else None)
    _write_json(paths.perception_last_seen_file(repo), current)  # mark seen regardless, so deltas are per-turn
    if not lines:
        return {}
    digest = (
        "Repo perception update (since last turn):\n- "
        + "\n- ".join(lines)
        + "\nThe harness auto-runs these checks in the background; call repo_state for the full snapshot."
    )
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": digest[:9000]}}


# SessionStart symbol map: a shallow, top-level-only tree of the repo's public shape, so a
# fresh session orients without a round of discovery reads. Bounded like recall — local and
# fail-open, never blocking startup.
_SYMBOLS_LIMIT = 150
_SYMBOLS_MAX_FILES = 40
_SYMBOLS_MAX_CHARS = 3500


def _symbol_lines(result: object) -> list[str]:
    """Render a shallow (top-level only) symbol map into displayable lines.

    Keeps only records with ``parent is None`` — one flat pass over the public shape, no
    method-level noise — and renders ``path: name(kind) — <doc>`` (doc omitted when absent).
    Bounded to ``_SYMBOLS_MAX_FILES`` files and ``_SYMBOLS_MAX_CHARS`` total characters.
    """
    symbols = getattr(result, "symbols", None)
    if not isinstance(symbols, dict):
        return []
    lines: list[str] = []
    files = 0
    total = 0
    for path, records in symbols.items():
        tops = [r for r in records if getattr(r, "parent", None) is None]
        if not tops:
            continue
        if files >= _SYMBOLS_MAX_FILES:
            break
        files += 1
        for r in tops:
            line = f"{path}: {r.name}({r.kind})"
            doc = getattr(r, "doc", None)
            if doc and doc.strip():
                line += f" — {doc.strip()[:70]}"
            if total + len(line) > _SYMBOLS_MAX_CHARS:
                return lines
            lines.append(line)
            total += len(line)
    return lines


# Hook-degradation warning (session_start section [0b]). Warn only for the events whose
# silent death actually degrades a session; session-start is the one running right now.
# Thresholds are hardcoded by design — this is a tripwire, not a tunable.
_HEARTBEAT_WARN_EVENTS = ("pre-tool-use", "post-tool-use", "user-prompt-submit")
_HEARTBEAT_MIN_SESSIONS = 3  # fresh install: too little history to tell "dead" from "new"
_HEARTBEAT_STALE_S = 7 * 24 * 3600


def session_start(data: dict, root: str | None = None) -> dict:
    """Inject session-start ``additionalContext`` from independent, fail-open sections.

    In order: a hook-heartbeat degradation warning and a shallow repo symbol map. Each
    section is computed independently and fails open to nothing, so a memory problem never
    delays or breaks session startup. Returns ``{}`` only when every section is empty.
    """
    _ = data
    repo = root or git.repo_root()
    if not repo:
        return {}
    name = Path(repo).name
    sections: list[str] = []

    # [0b] Hook-degradation warning — the shims fail open by contract, so a silently dead
    # hook (broken shim, stale venv) leaves no error anywhere; comparing heartbeats at
    # session start is the one moment the agent can be told. Skipped on fresh installs
    # (fewer than _HEARTBEAT_MIN_SESSIONS recorded session starts: no history to judge).
    with suppress(Exception):
        beats = paths.read_hook_heartbeats(repo)
        ss_beat = beats.get("session-start")
        if ss_beat and ss_beat["count"] >= _HEARTBEAT_MIN_SESSIONS:
            now = time.time()
            stale = [
                ev
                for ev in _HEARTBEAT_WARN_EVENTS
                if ev not in beats or (beats[ev]["ts"] < ss_beat["ts"] and now - beats[ev]["ts"] > _HEARTBEAT_STALE_S)
            ]
            if stale:
                sections.append(
                    "Hook heartbeat warning: no recent successful run recorded for "
                    + ", ".join(stale)
                    + " — the fail-open hook shims may be silently broken; check the plugin hook wiring."
                )

    # [1] Repo symbol map — local, fail-open to nothing.
    with suppress(Exception):
        from repo_agent_harness import symbols  # noqa: PLC0415 - lazy: pulls in tree-sitter
        from repo_agent_harness.symbols import SymbolsOverviewIn  # noqa: PLC0415 - lazy

        res = symbols.overview(repo, SymbolsOverviewIn(path=None, limit=_SYMBOLS_LIMIT))
        lines = _symbol_lines(res)
        if lines:
            sections.append(f"Repo symbol map ({name}):\n- " + "\n- ".join(lines))

    if not sections:
        return {}
    ctx = "\n\n".join(sections)[:9000]
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}


_HANDLERS = {
    "pre-tool-use": pre_tool_use,
    "post-tool-use": post_tool_use,
    "user-prompt-submit": user_prompt_submit,
    "session-start": session_start,
    "antigravity-pre-tool-use": antigravity_pre_tool_use,
    "antigravity-post-tool-use": antigravity_post_tool_use,
}


def dispatch(event: str, data: dict) -> dict:
    """Route one hook event: resolve the repo root once, run the handler, stamp the heartbeat.

    The stamp records "this handler ran to completion" (the fail-open shims leave no other
    trace of success); it is written only after the handler returns and under suppress, so
    a heartbeat problem can never alter the decision JSON. Unknown events keep the historic
    default (pre-tool-use) and stamp under that resolved name so free-form typos don't
    pollute the heartbeat list. Handler exceptions propagate — the stdin/stdout wrappers
    (``main`` below, ``cli._hook``) own the fail-open contract.
    """
    root: str | None = None
    with suppress(Exception):  # repo_root returns None outside a repo; suppress is belt-and-braces
        root = git.repo_root()
    resolved = event if event in _HANDLERS else "pre-tool-use"
    out = _HANDLERS[resolved](data, root=root)
    if root:
        with suppress(Exception):  # a stamp failure must never alter the decision JSON
            paths.stamp_hook_heartbeat(root, resolved)
    return out


def main(argv: list[str] | None = None) -> int:
    """Lightweight hook entry: ``python -m repo_agent_harness.agent_hooks <event>``.

    The plugin's PreToolUse shim calls this instead of ``repo-agent-harness hook`` so it imports
    only this module (and git/policies/secrets), not the full CLI graph (health,
    verify, …) — ~40ms vs ~600ms per tool call. Reads the event JSON on stdin, prints the
    decision JSON, routing through ``dispatch`` (shared with ``cli._hook``) so both entries stamp
    heartbeats. Fail-open by contract: any error prints an empty response and exits 0.
    """
    args = sys.argv[1:] if argv is None else argv
    event = args[0] if args else "pre-tool-use"
    try:
        data = json.load(sys.stdin)
        out = dispatch(event, data)
    except Exception:  # noqa: BLE001 — fail-open contract: any error must yield an empty allow
        out = {}
    print(json.dumps(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
