"""Ship claude-mem rows into cognee on a background cadence (the coexistence sync loop).

This replaces the capture-drain as the server's memory-ingest task. Where the drain read a
local WAL *queue* fed by hooks and digested each batch through a model, CogneeSync is a pure
mirror: it reads the live claude-mem store read-only (:mod:`claude_mem_reader`), and ships
verbatim rendered docs to cognee via the shared HTTP client. It owns NO model, NO child
process, NO agent runtime — the only things it touches are the cognee HTTP client, the
read-only claude-mem reader, our writable :class:`SyncLedger`, and path helpers. That is
invariant I2: nothing on this call path may reach an LLM or a spawned process.

Lifecycle is the standard ``start(client)`` / ``stop()`` task shape, so the ``_lifespan``
wiring is a one-liner. Per-repo scoped by ``root`` — one repo's claude-mem project into that
repo's ``cm_<project>`` dataset, never a global all-projects sweep.

Contract with :class:`SyncLedger` (see its ``watermark`` docstring): ship rows strictly in
``source_id`` order and STOP a kind's batch on the first failure, so the watermark stays a
true contiguous low-water mark. A failure leaves the watermark unmoved; the next cycle re-reads
and re-ships from the same point (at-least-once). ``already_ok`` only skips docs that verified
ok in a *prior* cycle, so a re-shipped failed batch relies on cognee-side dedup of the identical
provenance-stamped text — not our ledger — to avoid graph duplication.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from repo_agent_harness import claude_mem_reader, paths
from repo_agent_harness.capture import CAPTURE_NODE_SET  # SSOT for the ship node_set (value ["session_digest"])
from repo_agent_harness.cognee_client import CogneeError
from repo_agent_harness.sync_ledger import VERIFY_OK, SyncLedger

if TYPE_CHECKING:
    from collections.abc import Callable

    from repo_agent_harness.claude_mem_reader import MemDoc
    from repo_agent_harness.cognee_client import CogneeClient, Json

LOG = logging.getLogger(__name__)

# Cadence: one cycle immediately on start (catch-up), then every _POLL_SECONDS.
_POLL_SECONDS = 60.0
# At most 20 docs per remember (one in-flight remember at a time — the sequential loop below
# already guarantees that budget: never a second remember before the first has verified).
_BATCH_SIZE = 20
# Bound the post-remember completion poll so a stuck cognify never wedges a cycle.
_POLL_BUDGET_S = 120.0
_POLL_INTERVAL_S = 2.0

# Ship-status labels recorded in the ledger. Only VERIFY_OK advances the watermark.
_STATUS_TIMEOUT = "timeout"
_STATUS_ERROR = "error"

# Dataset sanitiser: cm_<project> lowercased with every non [a-z0-9_] char collapsed to _.
_DATASET_UNSAFE = re.compile(r"[^a-z0-9_]")

# Env override for the claude-mem store location (tests point this at a fixture DB).
_CLAUDE_MEM_DB_ENV = "CLAUDE_MEM_DB"


def _default_claude_mem_db() -> Path:
    """The live claude-mem store, ``~/.claude-mem/claude-mem.db`` unless overridden by env."""
    env = (os.environ.get(_CLAUDE_MEM_DB_ENV) or "").strip()
    return Path(env) if env else Path.home() / ".claude-mem" / "claude-mem.db"


def _dataset_for(project: str) -> str:
    """The per-repo ship dataset: ``cm_<sanitized project>`` (e.g. ``astrojones`` -> ``cm_astrojones``)."""
    return "cm_" + _DATASET_UNSAFE.sub("_", project.lower())


class SyncBreaker:
    """Sync-cycle-level breaker: 5 consecutive failed cycles -> open 10 min -> half-open retry.

    Distinct from :class:`cognee_client.CogneeCircuit` (which is transport/request scoped): this
    trips on whole failed *cycles* so a persistently-down cognee stops the remember/poll work
    entirely for a cool-off window instead of retrying every 60s. Mirrors CogneeCircuit's
    injectable-clock shape so tests advance time without sleeping.
    """

    def __init__(
        self,
        threshold: int = 5,
        open_seconds: float = 600.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start closed; ``clock`` is injectable so tests advance time without sleeping."""
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        """One of ``closed`` / ``open`` / ``half_open`` (cool-off window elapsed)."""
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._open_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """Whether this cycle may do remember/poll work (closed, or the half-open retry)."""
        return self.state != "open"

    def record_success(self) -> None:
        """Reset to closed."""
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """Count a failed cycle; trip (or re-trip after a failed retry) at the threshold."""
        self._failures += 1
        if self._failures >= self._threshold or self._opened_at is not None:
            self._opened_at = self._clock()


class CogneeSync:
    """Owns the per-repo claude-mem -> cognee mirror task (``start(client)`` / ``stop()``).

    Started from ``_lifespan`` with the shared cognee client the moment a backend is
    configured (remote now, or a local container once ``_bring_up_local`` rebuilds the client).
    Fail-open (I6): a cycle exception is swallowed and logged, never propagated — the sync task
    must never crash the daemon.
    """

    def __init__(  # noqa: PLR0913 - keyword-only test/injection knobs, all with defaults
        self,
        root: str | None,
        *,
        db: Path | None = None,
        project: str | None = None,
        ledger: SyncLedger | None = None,
        clock: Callable[[], float] = time.monotonic,
        poll_seconds: float = _POLL_SECONDS,
    ) -> None:
        """Bind to a worktree; resolve project/dataset purely (no I/O). Ledger/client are lazy."""
        self._root = root
        self._db = db if db is not None else _default_claude_mem_db()
        # Project == repo basename by default (baseline data: this repo's claude-mem project is
        # its basename, e.g. 'astrojones'). FLAGGED assumption — no canonical project helper exists.
        self._project = project if project is not None else (Path(root).name if root else "")
        self._dataset = _dataset_for(self._project)
        self._ledger = ledger
        self._breaker = SyncBreaker(clock=clock)
        self._poll_seconds = poll_seconds
        self._client: CogneeClient | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self, client: CogneeClient) -> None:
        """Start the mirror loop against ``client`` unless there is no repo or one is already running."""
        if self._root is None or self._task is not None:
            return
        self._stop.clear()  # allow a clean restart after a prior stop() (else run() would exit at once)
        self._bind(client)
        self._task = asyncio.create_task(self.run())

    async def stop(self) -> None:
        """Signal the loop to exit and await it (no-op when it never started)."""
        self._stop.set()
        task, self._task = self._task, None
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    def _bind(self, client: CogneeClient) -> None:
        """Attach the client and materialise the ledger (kept out of ``__init__`` for lazy I/O)."""
        self._client = client
        if self._ledger is None:
            self._ledger = SyncLedger()

    async def run(self) -> None:
        """Background loop: a cycle immediately (catch-up), then one every ``poll_seconds``."""
        while not self._stop.is_set():
            try:
                await self._cycle()
            except Exception:  # noqa: BLE001 - I6 fail-open: a cycle failure must never crash the server
                LOG.debug("cognee sync cycle failed for %s", self._root, exc_info=True)
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop.wait(), self._poll_seconds)

    # ------------------------------------------------------------------ one cycle

    async def _cycle(self) -> None:
        """One mirror pass: heartbeat, then (unless the breaker is open) ship both kinds in order."""
        if self._root is not None:
            paths.stamp_hook_heartbeat(self._root, "cm-sync")
        if not self._breaker.allow():
            return  # breaker open: skip all remember/poll work until the cool-off elapses
        ok = True
        for kind in ("obs", "sum"):
            if not await self._sync_kind(kind):
                ok = False
                break  # a down backend fails the whole cycle; don't hammer the other kind
        if ok:
            self._breaker.record_success()
        else:
            self._breaker.record_failure()

    async def _sync_kind(self, kind: str) -> bool:
        """Ship one kind's docs past its watermark, in id order, batched. False on any failure.

        Returns True when everything (possibly nothing) shipped cleanly; False the moment a
        batch fails — the caller then STOPS, leaving the watermark unmoved for the next cycle.
        """
        assert self._ledger is not None  # noqa: S101 - _bind ran before any cycle
        reader = claude_mem_reader.read_observations if kind == "obs" else claude_mem_reader.read_summaries
        watermark = self._ledger.watermark(kind)
        docs = await asyncio.to_thread(reader, self._db, self._project, watermark)
        batch: list[MemDoc] = []
        for doc in docs:
            if self._ledger.already_ok(doc.content_hash):
                continue  # replay dedup: an identical doc already verified ok, skip re-shipping
            batch.append(doc)
            if len(batch) >= _BATCH_SIZE:
                if not await self._ship_batch(kind, batch):
                    return False
                batch = []
        if batch:
            return await self._ship_batch(kind, batch)
        return True

    async def _ship_batch(self, kind: str, batch: list[MemDoc]) -> bool:
        """Remember one batch, poll to completion, then record every doc. All-or-nothing.

        On ANY failure (remember error, poll timeout/error) NO doc is recorded ``ok`` — the
        batch is recorded with the failure status (audit trail; a non-ok row never advances the
        watermark and never dedups a later retry) and False is returned so the caller stops.
        """
        assert self._ledger is not None  # noqa: S101 - _bind ran before any cycle
        assert self._client is not None  # noqa: S101 - _bind ran before any cycle
        texts = [doc.text for doc in batch]
        status = await self._ship_and_verify(texts)
        shipped_at = time.time()
        for doc in batch:
            self._ledger.record(kind, doc.source_id, doc.content_hash, self._dataset, status, shipped_at)
        return status == VERIFY_OK

    async def _ship_and_verify(self, texts: list[str]) -> str:
        """POST /remember then poll /datasets/status; return ``ok`` | ``timeout`` | ``error``."""
        assert self._client is not None  # noqa: S101 - _bind ran before any cycle
        try:
            resp = await self._client.remember(texts, self._dataset, list(CAPTURE_NODE_SET), run_in_background=True)
        except CogneeError:
            LOG.debug("cognee remember failed for %s", self._dataset, exc_info=True)
            return _STATUS_ERROR
        dataset_id = self._extract_dataset_id(resp)
        if dataset_id is None:
            LOG.debug("cognee remember returned no dataset id for %s", self._dataset)
            return _STATUS_ERROR
        return await self._poll(dataset_id)

    async def _poll(self, dataset_id: str) -> str:
        """Poll cognify_status up to ``_POLL_BUDGET_S`` (wall clock): ``ok`` on completion else fail."""
        assert self._client is not None  # noqa: S101 - _bind ran before any cycle
        deadline = time.monotonic() + _POLL_BUDGET_S
        while True:
            try:
                resp = await self._client.cognify_status(dataset_id)
            except CogneeError:
                LOG.debug("cognee cognify_status failed for %s", dataset_id, exc_info=True)
                return _STATUS_ERROR
            state = self._extract_status(resp).upper()
            if "COMPLETED" in state:
                return VERIFY_OK
            if "ERROR" in state or "FAILED" in state:
                return _STATUS_ERROR
            if time.monotonic() >= deadline:
                return _STATUS_TIMEOUT
            await asyncio.sleep(_POLL_INTERVAL_S)

    # ------------------------------------------------------------------ response parsing
    # FLAG: both extractors are fake-driven (tests) — the live remember-response dataset-id
    # field name and the cognify_status completion-field shape are NOT confirmed against the
    # live cognee OpenAPI. Kept lenient (multiple candidate keys) pending live verification.

    @staticmethod
    def _extract_dataset_id(resp: Json) -> str | None:
        """The dataset id echoed by /remember, trying the likely field spellings."""
        candidates = ("dataset_id", "datasetId", "id", "dataset")
        if isinstance(resp, dict):
            for key in candidates:
                value = resp.get(key)
                if isinstance(value, str) and value:
                    return value
        if isinstance(resp, list) and resp and isinstance(resp[0], dict):
            for key in candidates:
                value = resp[0].get(key)
                if isinstance(value, str) and value:
                    return value
        return None

    @staticmethod
    def _extract_status(resp: Json) -> str:
        """Flatten a /datasets/status body to a single string for COMPLETED/ERROR matching."""
        if isinstance(resp, dict):
            if "status" in resp:
                return str(resp["status"])
            parts = [str(v.get("status")) if isinstance(v, dict) and "status" in v else str(v) for v in resp.values()]
            return " ".join(parts)
        return str(resp)
