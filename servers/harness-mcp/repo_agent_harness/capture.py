"""Async capture pipeline: hooks enqueue locally (sqlite), a background drain ships to cognee.

The hard rule of the memory design: **capture hooks never block a turn on an LLM or network
call**. Hooks (stop / pre-compact / post-tool-use piggyback) do exactly one sqlite
INSERT-commit-close into a per-worktree WAL queue — sub-10ms, durable, fail-open. The
long-lived MCP server drains that queue in-process (:class:`BrainCapture`, registered in
``_lifespan`` beside the perception daemon — deliberately NOT a detached daemon), optionally
digesting raw entries into an observation summary first (``BRAIN_DIGEST_MODEL``), then ships
via the shared cognee client. Rows are deleted only after a successful ship, so a crash or a
cognee outage merely leaves the queue to resume on the next server start — the accepted cost:
no drain runs between sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import sqlite3
import time
from typing import TYPE_CHECKING

from repo_agent_harness import paths

if TYPE_CHECKING:
    from pathlib import Path

    from repo_agent_harness.cognee_client import CogneeClient

LOG = logging.getLogger(__name__)

CAPTURE_DATASET = "agent_sessions"
CAPTURE_NODE_SET = ["agent_actions"]

_BUSY_TIMEOUT_MS = 5000
_MAX_PAYLOAD_CHARS = 8_000
# Backstop against a never-draining queue (cognee never configured / down for weeks):
# enqueue prunes the oldest rows beyond this cap, so the local footprint stays bounded.
_MAX_QUEUE_ROWS = 10_000

# Digest-on-drain: summarize a raw batch into one observation digest before ingest, via the
# locally-authenticated claude CLI (subscription auth — zero cognee server cost). 'off'
# ships the raw entries; any digest failure falls back to raw. Runs inside the async drain
# ONLY — never on a hook path.
DIGEST_MODEL_ENV = "BRAIN_DIGEST_MODEL"
DIGEST_MODEL_DEFAULT = "claude-sonnet-5"
_DIGEST_TIMEOUT_S = 120.0
_DIGEST_PROMPT = (
    "You are compressing an agent-session capture log into a durable memory observation. "
    "Summarize the entries below into a compact digest: what was worked on, decisions made, "
    "files touched, and outcomes. Keep concrete identifiers (paths, commits, tool names). "
    "Output only the digest text.\n\nEntries:\n"
)


def queue_db(root: str) -> Path:
    """Path of the per-worktree capture queue database."""
    d = paths.repo_state_dir(root) / "brain"
    d.mkdir(parents=True, exist_ok=True)
    return d / "capture_queue.db"


def _connect(db: Path) -> sqlite3.Connection:
    """Open the queue with WAL + busy timeout so hook writers and the drain never deadlock."""
    conn = sqlite3.connect(db, timeout=_BUSY_TIMEOUT_MS / 1000)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS capture_queue ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, created_at REAL NOT NULL, "
        "event TEXT NOT NULL, payload TEXT NOT NULL)"
    )
    return conn


def enqueue(root: str, event: str, payload: dict | None) -> None:
    """One INSERT-commit-close from a hook. Fail-open: never raises, never touches the network."""
    try:
        text = json.dumps(payload or {})[:_MAX_PAYLOAD_CHARS]
        with contextlib.closing(_connect(queue_db(root))) as conn, conn:
            conn.execute(
                "INSERT INTO capture_queue (created_at, event, payload) VALUES (?, ?, ?)",
                (time.time(), event, text),
            )
            conn.execute(
                "DELETE FROM capture_queue WHERE id NOT IN (SELECT id FROM capture_queue ORDER BY id DESC LIMIT ?)",
                (_MAX_QUEUE_ROWS,),
            )
    except Exception:  # noqa: BLE001 - a capture failure must never block or break a turn
        LOG.debug("capture enqueue failed for %s", root, exc_info=True)


def pending_count(root: str) -> int:
    """Rows currently queued (0 on any error)."""
    try:
        with contextlib.closing(_connect(queue_db(root))) as conn:
            return int(conn.execute("SELECT COUNT(*) FROM capture_queue").fetchone()[0])
    except Exception:  # noqa: BLE001 - diagnostics only; uncertainty reads as empty
        return 0


class BrainCapture:
    """Owns the per-worktree drain loop: poll the queue, batch, digest, ship, delete.

    Mirrors :class:`perception.Perception`'s lifecycle shape (``run()`` task + ``stop()``),
    registered in the server's ``_lifespan``. Ships through the shared process client, so
    circuit-breaker state is common with the mem_* tools; a failed ship keeps the rows and
    the loop simply retries on a later poll.
    """

    def __init__(
        self,
        root: str,
        client: CogneeClient,
        poll_seconds: float = 7.0,
        batch_size: int = 50,
    ) -> None:
        """Bind to a worktree and the shared cognee client; no I/O happens here."""
        self.root = root
        self._client = client
        self._poll_seconds = poll_seconds
        self._batch_size = batch_size
        self._stop = asyncio.Event()

    def stop(self) -> None:
        """Signal the drain loop to exit (wakes the poll sleep)."""
        self._stop.set()

    async def run(self) -> None:
        """Background loop: drain a batch, then sleep until the next poll or stop."""
        while not self._stop.is_set():
            try:
                shipped = await self._drain_once()
            except Exception:  # noqa: BLE001 - drain failure must never crash the server
                LOG.debug("capture drain failed for %s", self.root, exc_info=True)
                shipped = 0
            # An empty or failed pass waits the full poll; after a full batch keep draining.
            if shipped < self._batch_size:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), self._poll_seconds)

    async def _drain_once(self) -> int:
        """Ship one batch: read rows, digest (optional), add + background cognify, delete rows."""
        rows = await asyncio.to_thread(self._fetch_batch)
        if not rows:
            return 0
        entries = [self._render(created_at, event, payload) for _, created_at, event, payload in rows]
        digest = await _maybe_digest(entries)
        items = [digest] if digest else entries
        await self._client.add(items, CAPTURE_DATASET, CAPTURE_NODE_SET, run_in_background=False)
        await self._client.cognify(CAPTURE_DATASET, run_in_background=True)
        await asyncio.to_thread(self._delete, [row_id for row_id, *_ in rows])
        return len(rows)

    def _fetch_batch(self) -> list[tuple[int, float, str, str]]:
        with contextlib.closing(_connect(queue_db(self.root))) as conn:
            cur = conn.execute(
                "SELECT id, created_at, event, payload FROM capture_queue ORDER BY id LIMIT ?",
                (self._batch_size,),
            )
            return [(int(i), float(t), str(e), str(p)) for i, t, e, p in cur.fetchall()]

    def _delete(self, row_ids: list[int]) -> None:
        with contextlib.closing(_connect(queue_db(self.root))) as conn, conn:
            conn.executemany("DELETE FROM capture_queue WHERE id = ?", [(i,) for i in row_ids])

    @staticmethod
    def _render(created_at: float, event: str, payload: str) -> str:
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(created_at))
        return f"[{stamp}] {event}: {payload}"


def digest_model() -> str | None:
    """The digest model from ``BRAIN_DIGEST_MODEL`` (default sonnet-5), or None when 'off'."""
    raw = (os.environ.get(DIGEST_MODEL_ENV) or DIGEST_MODEL_DEFAULT).strip()
    return None if raw.lower() in {"off", "0", "false", ""} else raw


async def _maybe_digest(entries: list[str]) -> str | None:
    """Digest raw entries into one observation via the claude CLI; None = ship raw (fallback).

    Uses the locally-authenticated ``claude`` CLI in print mode (subscription auth,
    claude-mem's proven pattern) so the cognee server never pays extraction tokens for raw
    noise. Bounded and fail-open: a missing CLI, non-zero exit, timeout, or empty output all
    fall back to shipping the raw entries.
    """
    model = digest_model()
    if model is None or not entries:
        return None
    prompt = _DIGEST_PROMPT + "\n".join(entries)
    try:
        proc = await asyncio.create_subprocess_exec(
            "claude",
            "-p",
            prompt,
            "--model",
            model,
            "--output-format",
            "text",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except OSError:
        return None  # CLI not installed — raw entries it is
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), _DIGEST_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        return None
    if proc.returncode != 0:
        return None
    text = out.decode("utf-8", errors="replace").strip()
    return text or None
