"""Stable paths for harness persistent state (~/.harness by default)."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path


def repo_id(root: str) -> str:
    """Short stable identifier for the repo (SHA-256 of realpath, first 12 hex chars)."""
    return hashlib.sha256(os.path.realpath(root).encode()).hexdigest()[:12]


def harness_home() -> Path:
    """Root of harness persistent state; override via REPO_AGENT_HARNESS_HOME."""
    env = os.environ.get("REPO_AGENT_HARNESS_HOME")
    return Path(env) if env else Path.home() / ".harness"


def repo_state_dir(root: str) -> Path:
    """Per-repo state dir under harness_home(); created lazily with mode 0700."""
    d = harness_home() / "repos" / repo_id(root)
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


# Perception-layer state files under repo_state_dir(). Named here (in this light module,
# no heavy imports) so the daemon (perception.py) and the delivery hooks (agent_hooks.py)
# agree on locations without the hook hot-path importing the daemon.
PERCEPTION_FILE = "perception.json"
PERCEPTION_LAST_SEEN_FILE = "perception_last_seen.json"
PERCEPTION_TOUCHED_FILE = "perception_touched.json"


def perception_file(root: str) -> Path:
    """Path to the current perception snapshot written by the daemon."""
    return repo_state_dir(root) / PERCEPTION_FILE


def perception_last_seen_file(root: str) -> Path:
    """Path to the marker recording the snapshot last surfaced to the agent (UserPromptSubmit)."""
    return repo_state_dir(root) / PERCEPTION_LAST_SEEN_FILE


def perception_touched_file(root: str) -> Path:
    """Path to the set of files the agent has edited this session (PostToolUse attribution)."""
    return repo_state_dir(root) / PERCEPTION_TOUCHED_FILE


# Hook/job heartbeats under repo_state_dir()/heartbeats/ — one file per event so parallel
# hooks for *different* events never contend. A stamp means "this handler (or async job)
# ran to completion"; it is the observability answer to the silent-hook-death incident
# (the shims fail open by design, so success must leave a trace somewhere).
HEARTBEAT_DIR = "heartbeats"
HOOK_EVENTS = (
    "pre-tool-use",
    "post-tool-use",
    "user-prompt-submit",
    "session-start",
    "stop",
    "pre-compact",
)


def hook_heartbeat_file(root: str, event: str) -> Path:
    """Path to one event's (or job's) heartbeat marker; parent dir created lazily."""
    d = repo_state_dir(root) / HEARTBEAT_DIR
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{event}.json"


def stamp_hook_heartbeat(root: str, event: str) -> None:
    """Record a successful run of ``event`` as ``{"ts": epoch, "count": n}``.

    Atomic via temp + os.replace (readers never see a torn file). ``count`` is
    best-effort: parallel same-event stampers read-modify-write without a lock and
    may lose increments — treat it as a lower bound (fine for thresholds like
    "ran at least N times"); ``ts`` carries the freshness semantics, and tests
    must only assert on ``ts``/existence. The event string is free-form on
    purpose: async jobs (memify, migrations) stamp through the same helper so
    repo_health shows hook and job freshness uniformly.
    """
    path = hook_heartbeat_file(root, event)
    count = 0
    try:
        with path.open(encoding="utf-8") as f:
            prev = json.load(f)
        if isinstance(prev, dict):
            count = int(prev.get("count", 0))
    except (OSError, ValueError, TypeError):
        count = 0
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"ts": time.time(), "count": count + 1}), encoding="utf-8")
    tmp.replace(path)


def read_hook_heartbeats(root: str) -> dict[str, dict]:
    """All well-formed heartbeat markers as ``{event: {"ts": float, "count": int}}``.

    Fail-open: a missing dir yields ``{}``; a garbage marker is simply excluded, so
    consumers (health, session-start degradation warnings) treat it as "never ran".
    """
    try:
        files = list((repo_state_dir(root) / HEARTBEAT_DIR).glob("*.json"))
    except OSError:
        return {}
    beats: dict[str, dict] = {}
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("ts"), int | float):
                beats[f.stem] = {"ts": float(data["ts"]), "count": int(data.get("count", 0))}
        except (OSError, ValueError, TypeError):
            continue
    return beats


# Cognee onboarding marker under repo_state_dir(). Records that this repo's memory graph has
# been bootstrapped, so the daemon/hooks skip re-onboarding. Lives beside symbols.json, so it
# is gitignored-by-construction (outside the working tree).
COGNEE_ONBOARDED_FILE = "cognee_onboarded.json"


def cognee_onboarded_file(root: str) -> Path:
    """Path to the marker recording that this repo has been onboarded into cognee."""
    return repo_state_dir(root) / COGNEE_ONBOARDED_FILE


def is_cognee_onboarded(root: str) -> bool:
    """True iff the marker exists and parses as JSON; fail-closed to False on any error."""
    try:
        with cognee_onboarded_file(root).open(encoding="utf-8") as f:
            json.load(f)
    except (OSError, ValueError):
        return False
    return True


def onboarded_dataset(root: str) -> str | None:
    """The project dataset recorded at onboarding, or None if unmarked/unreadable.

    Written by mark_cognee_onboarded(dataset=…). Lets session-start recall scope to the
    project's own dataset instead of fanning across every dataset; None means "not
    recorded", so the caller falls back to the user's default (span-all) scope — which is
    what un-onboarded and multi-repo-shared-dataset repos rely on. Fail-closed to None.
    """
    try:
        with cognee_onboarded_file(root).open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    ds = data.get("dataset") if isinstance(data, dict) else None
    return ds if isinstance(ds, str) and ds else None


def mark_cognee_onboarded(root: str, **meta: object) -> None:
    """Write the onboarding marker, recording the epoch time plus any provided metadata."""
    payload = {"onboarded_at": time.time(), **meta}
    cognee_onboarded_file(root).write_text(json.dumps(payload), encoding="utf-8")
