"""OUR writable ledger of claude-mem rows shipped to cognee (one row per shipped item).

Unlike ``claude_mem_reader`` (which is strictly read-only over someone else's live DB), this is
the harness's OWN database — free to create, write, and migrate. It lives beside the cognee
endpoint descriptor under ``harness_home()/cognee/sync_ledger.db`` (WAL mode) and owns the
watermark state the reader deliberately does not.

One row per shipped item records ``(kind, source_id, content_hash, dataset, shipped_at,
verify_status)``. The three primitives Phase-3 CogneeSync needs:

* ``watermark(kind)`` — the high-water source_id for that kind whose ship *verified ok* (0 when
  none), so the next pass reads only ``id > watermark``.
* ``record(...)`` — append one shipped item.
* ``already_ok(content_hash)`` — content-addressed dedup, uniform across observations (which have
  a DB content_hash) and summaries (which do not — the reader's render-hash gives both the same
  semantics).

``create_all`` is scoped to THIS table only (never a bare metadata create) so importing the
read-only reader — whose table models share ``SQLModel.metadata`` — can never cause the claude-mem
tables to be materialised in our ledger DB.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from sqlalchemy import func
from sqlmodel import Field, Session, SQLModel, create_engine, select

from repo_agent_harness import paths

if TYPE_CHECKING:
    from pathlib import Path

_WRITE_TIMEOUT_S = 10.0
VERIFY_OK = "ok"


class SyncItem(SQLModel, table=True):
    """One claude-mem row shipped to cognee (the ledger's single table)."""

    __tablename__ = "sync_ledger"

    id: int | None = Field(default=None, primary_key=True)
    kind: str = Field(index=True)  # "obs" | "sum"
    source_id: int  # the real observations.id / session_summaries.id
    content_hash: str = Field(index=True)
    dataset: str
    shipped_at: float
    verify_status: str  # "ok" | anything else (failed/pending)


class SyncLedger:
    """A thin store over ``sync_ledger.db``; the only component that writes watermark state."""

    def __init__(self, db: Path | None = None) -> None:
        """Open (creating if needed) the ledger DB, enabling WAL and creating only our table."""
        self._path = db or paths.sync_ledger_file()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._engine = create_engine(f"sqlite:///{self._path}", connect_args={"timeout": _WRITE_TIMEOUT_S})
        with self._engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        # Scoped to SyncItem only: the reader's table models share SQLModel.metadata, and a bare
        # create_all would try to materialise the claude-mem tables in our DB.
        SQLModel.metadata.create_all(self._engine, tables=[SQLModel.metadata.tables[SyncItem.__tablename__]])

    def watermark(self, kind: str, dataset: str) -> int:
        """The max ``source_id`` of a verified-ok row for ``kind`` in ``dataset`` (0 when none yet).

        Scoped by ``dataset``: the ledger is machine-global (one file for every repo's
        CogneeSync) and claude-mem ids are a global autoincrement whose ranges interleave
        across projects, so an unscoped watermark lets one dataset's high ids strand
        another's low ids. Each dataset carries its own independent watermark.

        CONTRACT for callers (CogneeSync, Phase 3): ship rows strictly in ``source_id``
        order and STOP the batch on the first failure — do NOT record a later ok row past
        a still-failing earlier one. Because reads are ``id > watermark`` and this returns
        the max ok id, a higher ok row would jump the watermark past the failed lower id,
        which would then never be re-read (content-hash dedup can't help a row never
        re-fetched). Processing in order + stop-on-failure keeps the watermark a true
        contiguous low-water mark.

        NOTE (recovery): rows stranded *below* this dataset's max-ok id — e.g. low ids never
        shipped because a pre-fix global watermark skipped them — are not re-read by normal
        cycles; recovering them needs an explicit watermark reset/backfill for the dataset.
        """
        with Session(self._engine) as session:
            stmt = select(func.max(SyncItem.source_id)).where(
                SyncItem.kind == kind, SyncItem.dataset == dataset, SyncItem.verify_status == VERIFY_OK
            )
            result = session.exec(stmt).one()
        return int(result) if result is not None else 0

    def record(
        self,
        kind: str,
        source_id: int,
        content_hash: str,
        dataset: str,
        verify_status: str = VERIFY_OK,
        shipped_at: float | None = None,
    ) -> None:
        """Append one shipped item; ``shipped_at`` defaults to now."""
        item = SyncItem(
            kind=kind,
            source_id=source_id,
            content_hash=content_hash,
            dataset=dataset,
            verify_status=verify_status,
            shipped_at=shipped_at if shipped_at is not None else time.time(),
        )
        with Session(self._engine) as session:
            session.add(item)
            session.commit()

    def already_ok(self, content_hash: str, dataset: str) -> bool:
        """Whether a verified-ok row already exists for ``content_hash`` in ``dataset`` (content-addressed dedup)."""
        with Session(self._engine) as session:
            stmt = select(SyncItem.id).where(
                SyncItem.content_hash == content_hash, SyncItem.dataset == dataset, SyncItem.verify_status == VERIFY_OK
            )
            return session.exec(stmt).first() is not None
