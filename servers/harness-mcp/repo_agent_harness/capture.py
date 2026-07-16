"""Async capture pipeline: hooks enqueue locally (sqlite), a background drain ships to cognee.

The hard rule of the memory design: **capture hooks never block a turn on an LLM or network
call**. Hooks (stop / pre-compact / post-tool-use piggyback) do exactly one sqlite
INSERT-commit-close into a per-worktree WAL queue — sub-10ms, durable, fail-open. The
long-lived MCP server drains that queue in-process (:class:`BrainCapture`, registered in
``_lifespan`` beside the perception daemon — deliberately NOT a detached daemon), optionally
digesting raw entries into an observation summary first via a pluggable backend (see
:mod:`repo_agent_harness.digest_providers`), then ships via the shared cognee client. Rows are
deleted only after a successful ship, so a crash or a cognee outage merely leaves the queue to
resume on the next server start — the accepted cost: no drain runs between sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import sqlite3
import time
from typing import TYPE_CHECKING

from repo_agent_harness import digest_providers, paths, secrets

if TYPE_CHECKING:
    from pathlib import Path

    from repo_agent_harness.cognee_client import CogneeClient

LOG = logging.getLogger(__name__)

# Ship target for NEW writes: the project's onboarded dataset when recorded, else this
# fallback; node_set base is session_digest (replaced agent_actions). Local constants for
# now — the mem.py constants table (stream S3) becomes the SSOT and the integrator re-points.
CAPTURE_DATASET = "agent_sessions"
CAPTURE_NODE_SET = ["session_digest"]

_BUSY_TIMEOUT_MS = 5000
_MAX_PAYLOAD_CHARS = 8_000
# Backstop against a never-draining queue (cognee never configured / down for weeks):
# enqueue prunes the oldest rows beyond this cap, so the local footprint stays bounded.
_MAX_QUEUE_ROWS = 10_000


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


@functools.lru_cache(maxsize=32)
def _secrets_cfg(root: str) -> secrets.SecretsConfig:
    """Redaction config via the full resolution chain (repo overrides > home > defaults).

    ``secrets.redact``'s default config knows only the builtin patterns; loading per-root
    keeps repo-specific patterns effective at the capture boundary. Cached because enqueue
    runs on the post-tool-use hot path and ``secrets.load`` probes the fs + parses yaml.
    """
    return secrets.load(root)


def enqueue(root: str, event: str, payload: dict | None) -> None:
    """One INSERT-commit-close from a hook. Fail-open: never raises, never touches the network."""
    try:
        # Redact before the row hits disk: digest and ship read the queue verbatim, so this
        # is the single scrub point. A redaction failure falls into the blanket except below
        # (row dropped) — safer than persisting an unscrubbed payload. Redact BEFORE the
        # cap: truncating first could slice a secret at the boundary so no pattern matches.
        text = secrets.redact(json.dumps(payload or {}), _secrets_cfg(root))[:_MAX_PAYLOAD_CHARS]
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
        result = await digest_providers.digest(entries)
        dataset = paths.onboarded_dataset(self.root) or CAPTURE_DATASET
        for node_set, docs in self._ship_groups(result, entries):
            await self._client.add(docs, dataset, node_set, run_in_background=False)
        await self._client.cognify(dataset, run_in_background=True)
        # memify distills CODING_RULES and consolidates entities server-side. Enrichment,
        # not shipping: a memify hiccup must never fail the drain (rows are already added),
        # so it runs fail-open, and only a successful run stamps the job heartbeat.
        with contextlib.suppress(Exception):
            await self._client.memify(dataset, run_in_background=True)
            paths.stamp_hook_heartbeat(self.root, "memify")
        await asyncio.to_thread(self._delete, [row_id for row_id, *_ in rows])
        return len(rows)

    @staticmethod
    def _ship_groups(
        result: digest_providers.DigestResult | None, entries: list[str]
    ) -> list[tuple[list[str], list[str]]]:
        """(node_set, docs) batches: observations grouped per tag combination, else one plain doc.

        Every branch fails open to shipping something — a digest outcome never loses rows.
        """
        if result is not None and result.observations:
            observed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            groups: dict[tuple[str, ...], list[str]] = {}
            for obs in result.observations:
                # sorted() canonicalizes the group key: identical concept sets in different
                # order must batch together instead of splitting into separate add() calls.
                node_set = (*CAPTURE_NODE_SET, f"type:{obs.type}", *(f"concept:{c}" for c in sorted(obs.concepts)))
                groups.setdefault(node_set, []).append(BrainCapture._observation_doc(obs, observed_at))
            return [(list(tags), docs) for tags, docs in groups.items()]
        if result is not None and result.text:
            return [(CAPTURE_NODE_SET, [result.text])]
        return [(CAPTURE_NODE_SET, entries)]

    @staticmethod
    def _observation_doc(obs: digest_providers.DigestObservation, observed_at: str) -> str:
        """Render one observation as a markdown doc (Observed: is future-proofing, see plan)."""
        lines = [f"# {obs.type}: {obs.title}"]
        lines += [f"- {fact}" for fact in obs.facts]
        if obs.concepts:
            lines.append("Concepts: " + ", ".join(obs.concepts))
        if obs.files:
            lines.append("Files: " + ", ".join(obs.files))
        lines.append(f"Observed: {observed_at}")
        return "\n".join(lines)

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
