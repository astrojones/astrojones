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
import os
import re
import sys
import time
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from repo_agent_harness import git, paths, policies, secrets, serena_gate

if TYPE_CHECKING:
    from repo_agent_harness.cognee_client import CogneeClient

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


def _serena_gate_blocks(repo: str, path: str) -> tuple[bool, str]:
    """Return (blocks, message) deciding whether a native Read of ``path`` is denied.

    Denies reads of *code* files to keep code discovery on Serena; the message varies by
    onboarding status (serena_gate.UNBOARDED_MSG vs BOARDED_MSG). Fails OPEN for non-code
    files, paths outside the repo, the env escape, or any error. The same predicate gates
    repo_read_range in server.py, so no ungated whole-file code path is left open.
    """
    if serena_gate.gate_disabled():
        return False, ""
    try:
        target = Path(path).resolve()
        rootp = Path(repo).resolve()
        if rootp != target and rootp not in target.parents:
            return False, ""  # outside the repo — not our concern
        if not serena_gate.is_code_file(target):
            return False, ""  # non-code files are always readable
        msg = serena_gate.UNBOARDED_MSG if not serena_gate.is_onboarded(rootp) else serena_gate.BOARDED_MSG
        return True, msg  # noqa: TRY300 — intentional return in try; fallthrough catch is fail-open
    except OSError:
        return False, ""  # fail open


def pre_tool_use(data: dict, root: str | None = None) -> dict:
    """Deny dangerous shell commands, secret-path reads, and ungated code reads via repo policy."""
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
            if tool == "Read" and repo is not None:
                blocks, msg = _serena_gate_blocks(repo, path)
                if blocks:
                    return _deny(msg)

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
        _capture(repo, "post_tool_use", {"tool_name": data.get("tool_name", ""), "path": path})
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


def _capture(repo: str, event: str, payload: dict) -> None:
    """Enqueue a capture row (local sqlite only — never a network call from a hook path).

    Skipped entirely when cognee is unconfigured, so setups without a memory backend never
    accumulate a queue nobody will ever drain.
    """
    if not (os.environ.get("COGNEE_BASE_URL") or "").strip():
        return
    with suppress(Exception):  # fail-open: capture must never block or break a turn
        from repo_agent_harness import capture  # noqa: PLC0415 - lazy, keeps the hot path light

        capture.enqueue(repo, event, payload)


def stop(data: dict, root: str | None = None) -> dict:
    """Stop hook: enqueue-only (zero synchronous HTTP); the server-side drain ships it later."""
    repo = root or git.repo_root()
    if repo:
        _capture(repo, "stop", data)
    return {}


def pre_compact(data: dict, root: str | None = None) -> dict:
    """PreCompact hook: enqueue-only (zero synchronous HTTP) — capture before context is squashed."""
    repo = root or git.repo_root()
    if repo:
        _capture(repo, "pre_compact", data)
    return {}


# SessionStart recall: the ONE hook allowed a network call, bounded and fail-open. Every
# other hook stays enqueue-only/local by hard rule — a turn must never block on cognee.
_RECALL_TIMEOUT_ENV = "REPO_AGENT_HARNESS_RECALL_TIMEOUT_S"
# Once per session, so a few seconds is acceptable. Recall uses CHUNKS (verbatim chunk
# retrieval, no server-side synthesis), but a live cold roundtrip still pays TLS + login +
# vector search; 8s leaves headroom over the observed ~3-4s cold path.
_RECALL_TIMEOUT_S = 8.0
_RECALL_TOP_K = 5
# A GRAPH_COMPLETION answer is one synthesized paragraph, not a short chunk — keep it whole
# (the section as a whole is still budgeted to 9000 chars in session_start).
_RECALL_LINE_CHARS = 1200
_RECALL_MAX_LINES = 8


# SessionStart symbol map: a shallow, top-level-only tree of the repo's public shape, so a
# fresh session orients without a round of discovery reads. Bounded like recall — local and
# fail-open, never blocking startup.
_SYMBOLS_LIMIT = 150
_SYMBOLS_MAX_FILES = 40
_SYMBOLS_MAX_CHARS = 3500


# C0/C1 controls minus tab/newline (legit whitespace, collapsed by the callers); \r is
# stripped too — carriage returns enable terminal-overwrite tricks in injected context.
_CONTROL_CHARS_RX = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _sanitize_line(text: str) -> str:
    """Neutralize recall text before it enters the injected session context.

    Graph content is externally influenced (captured tool output, ingested docs), so a
    poisoned line could carry ``<system-reminder>``/hook-style tag sequences that the model
    would treat as trusted framing. ``<`` becomes U+2039 (single left angle quote): unlike
    ``&lt;`` it stays readable as plain text (recall is not HTML), while making it
    impossible to form a tag. Control characters (ANSI escapes, NUL, C1) are stripped so
    graph content cannot drive the terminal; tab/newline survive as ordinary whitespace.
    """
    return _CONTROL_CHARS_RX.sub("", text.replace("<", chr(0x2039)))


def _recall_lines(results: object) -> list[str]:
    """Flatten a mem_search result payload into displayable lines (shape-tolerant).

    Handles the live shapes: a list of per-dataset dicts whose ``search_result`` holds
    either strings (completion answers) or chunk records with a ``text`` field. Every
    line passes through :func:`_sanitize_line` — graph content is untrusted input.
    """
    if isinstance(results, str):
        return [_sanitize_line(results.strip())] if results.strip() else []
    raw: list[object] = []
    if isinstance(results, list):
        for r in results:
            if isinstance(r, dict):
                sr = r.get("search_result") or r.get("text")
                raw.extend(sr if isinstance(sr, list) else [sr])
            else:
                raw.append(r)
    lines: list[str] = []
    for item in raw:
        text = item.get("text") if isinstance(item, dict) else item
        line = _sanitize_line(" ".join(str(text).split())) if text else ""
        if line:
            lines.append(line[:_RECALL_LINE_CHARS])
    return lines[:_RECALL_MAX_LINES]


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


def _recall_section(name: str, dataset: str | None, client: CogneeClient | None) -> str | None:
    """Bounded, fail-open durable-memory recall; returns the section text or ``None``.

    ``dataset`` scopes the query to the project's own cognee dataset (from the onboarding
    marker); ``None`` spans the user's default scope (every dataset), the fallback for repos
    not yet onboarded and for multi-repo projects that share one dataset.

    Uses CHUNKS filtered to the ``session_digest`` node set (``capture.CAPTURE_NODE_SET``):
    verbatim retrieval of this harness's own session digests, no server-side LLM synthesis.
    The node-set filter is what keeps recall clean — an unfiltered GRAPH_COMPLETION over the
    whole dataset also drags in cognee's own session-memory/self-improvement chatter (e.g. a
    stored "Got it."), whereas scoping to ``session_digest`` returns only distilled digests.
    Filtering verified against the live pgvector deployment.

    ``None`` whenever cognee is unconfigured, unreachable, times out, or yields nothing —
    the caller simply omits the section rather than aborting session start.
    """
    import asyncio  # noqa: PLC0415 - lazy: keep the sync hot-path hooks import-light

    try:
        from repo_agent_harness import capture, cognee_client, mem  # noqa: PLC0415 - lazy: pulls in httpx
        from repo_agent_harness.models import MemSearchIn, MemSearchResult  # noqa: PLC0415 - lazy
    except ImportError:
        return None
    c = client if client is not None else cognee_client.get_client()
    if not c.configured:
        return None
    query = f"Project {name}: recent work, decisions, open threads, and gotchas"
    inp = MemSearchIn(
        query=query,
        search_type="CHUNKS",
        dataset=dataset,
        top_k=_RECALL_TOP_K,
        node_name=list(capture.CAPTURE_NODE_SET),
    )
    timeout = float(os.environ.get(_RECALL_TIMEOUT_ENV, _RECALL_TIMEOUT_S))
    try:
        out = asyncio.run(asyncio.wait_for(mem.search(inp, client=c), timeout))
    except Exception:  # noqa: BLE001 - fail-open contract: recall must never break session start
        return None
    lines = _recall_lines(out.results) if isinstance(out, MemSearchResult) else []
    if not lines:
        return None
    return f"Durable-memory recall for {name} (cognee):\n- " + "\n- ".join(lines)


# Hook-degradation warning (session_start section [0b]). Warn only for the events whose
# silent death actually degrades a session; pre-compact fires too rarely to judge and
# session-start is the one running right now. Thresholds are hardcoded by design — this is
# a tripwire, not a tunable.
_HEARTBEAT_WARN_EVENTS = ("pre-tool-use", "post-tool-use", "user-prompt-submit", "stop")
_HEARTBEAT_MIN_SESSIONS = 3  # fresh install: too little history to tell "dead" from "new"
_HEARTBEAT_STALE_S = 7 * 24 * 3600


def session_start(data: dict, client: CogneeClient | None = None, root: str | None = None) -> dict:
    """Inject session-start ``additionalContext`` from independent, fail-open sections.

    In order: an onboarding nudge (if the repo isn't yet in durable memory), a hook-heartbeat
    degradation warning, a shallow repo symbol map, and a bounded durable-memory recall. Each
    section is computed independently and fails open to nothing, so a memory problem never
    delays or breaks session startup. Returns ``{}`` only when every section is empty.
    """
    _ = data
    repo = root or git.repo_root()
    if not repo:
        return {}
    name = Path(repo).name
    sections: list[str] = []

    # [0] Onboarding nudge — independent of cognee reachability/config, so it still fires
    # when cognee is unconfigured or down.
    with suppress(Exception):
        if not paths.is_cognee_onboarded(repo):
            sections.append("This repo isn't yet onboarded into durable memory — run /astrojones:onboard")

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

    # [2] Durable-memory recall — scoped to the project's own dataset when onboarded (the
    # marker records it), else the user's default span-all scope. Contributes nothing when
    # unconfigured/unreachable/empty.
    recall = _recall_section(name, paths.onboarded_dataset(repo), client)
    if recall:
        sections.append(recall)

    if not sections:
        return {}
    ctx = "\n\n".join(sections)[:9000]
    return {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}


_HANDLERS = {
    "pre-tool-use": pre_tool_use,
    "post-tool-use": post_tool_use,
    "user-prompt-submit": user_prompt_submit,
    "session-start": session_start,
    "stop": stop,
    "pre-compact": pre_compact,
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
    only this module (and git/policies/secrets/serena_gate), not the full CLI graph (gateway,
    health, verify, …) — ~40ms vs ~600ms per tool call. Reads the event JSON on stdin, prints the
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
