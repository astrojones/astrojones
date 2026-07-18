"""Strictly read-only view over the live claude-mem SQLite store (invariant I1).

The claude-mem DB (``~/.claude-mem/claude-mem.db``) is owned by another process and is a live
WAL database. This module NEVER writes to it — not a row, not a PRAGMA, not a checkpoint, not
schema DDL. It opens the file through SQLAlchemy's URI form with ``mode=ro`` so the kernel/SQLite
layer *rejects* any write, then reads ``observations`` / ``session_summaries`` for the current
repo's project past a caller-supplied watermark and renders one provenance-stamped text doc per
row for downstream cognee ingestion.

The SQLModel table classes declare ONLY the columns this reader consumes, with nullability
matching the real store EXACTLY: every column that is nullable in the live DB is ``Optional`` here,
so a real NULL row (e.g. ``observations.text`` on a body-less discovery) deserialises cleanly
instead of raising a Pydantic ``ValidationError`` on first contact with production data.

The reader is pure: the watermark is an input, never persisted here — the ``sync_ledger`` owns
watermark state. ``mode=ro`` (never ``immutable=1``) is deliberate: the source is a live WAL DB,
and ``immutable=1`` would risk stale reads / WAL corruption assumptions.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import TYPE_CHECKING, Literal

from pydantic import BaseModel
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlmodel import Field, Session, SQLModel, col, select

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from sqlalchemy import Engine

# Read tuning for a busy live WAL DB. A short busy-timeout lets SQLite wait out a writer's lock,
# and a small bounded backoff retries the rare "database is locked" that outlasts it.
_READ_TIMEOUT_S = 5.0
_RETRY_ATTEMPTS = 4
_RETRY_BASE_DELAY_S = 0.1


# --------------------------------------------------------------------------- table models
# nullability mirrors the real ~/.claude-mem/claude-mem.db EXACTLY: NOT NULL columns are required,
# every other column is Optional so a genuine NULL cell never crashes the reader.


class Observation(SQLModel, table=True):
    """Read-only projection of ``observations`` (only the columns the reader consumes)."""

    __tablename__ = "observations"

    id: int = Field(primary_key=True)
    project: str  # NOT NULL in the live store
    created_at: str  # NOT NULL (ISO-8601 text)
    title: str | None = None
    text: str | None = None
    narrative: str | None = None
    facts: str | None = None
    concepts: str | None = None
    files_read: str | None = None
    files_modified: str | None = None


class SessionSummary(SQLModel, table=True):
    """Read-only projection of ``session_summaries`` (only the columns the reader consumes)."""

    __tablename__ = "session_summaries"

    id: int = Field(primary_key=True)
    project: str  # NOT NULL in the live store
    created_at: str  # NOT NULL (ISO-8601 text)
    request: str | None = None
    investigated: str | None = None
    learned: str | None = None
    completed: str | None = None
    next_steps: str | None = None
    notes: str | None = None
    files_read: str | None = None
    files_edited: str | None = None


# --------------------------------------------------------------------------- public record


class MemDoc(BaseModel):
    """One rendered claude-mem row ready for cognee ingestion (the reader's public unit)."""

    kind: Literal["obs", "sum"]
    source_id: int
    content_hash: str
    text: str


# --------------------------------------------------------------------------- read-only engine


def _read_only_engine(db: Path) -> Engine:
    """A SQLAlchemy engine that can only READ ``db`` (``mode=ro``), never write it (invariant I1).

    ``mode=ro`` + ``uri=true`` make SQLite itself reject any write; ``immutable=1`` is deliberately
    NOT used because the source is a live WAL database. ``connect_args.timeout`` is SQLite's
    busy-timeout and coexists with the URI query string (proven by the read-only spike).
    """
    return create_engine(
        f"sqlite:///file:{db}?mode=ro&uri=true",
        connect_args={"timeout": _READ_TIMEOUT_S},
    )


def _with_retry(fn: Callable[[], list[MemDoc]]) -> list[MemDoc]:
    """Run ``fn`` with a small bounded backoff over the busy-lock of a live WAL DB.

    Only ``database is locked``/``busy`` operational errors are retried; anything else (including
    the "readonly database" a write attempt would raise) propagates immediately.
    """
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return fn()
        except OperationalError as exc:
            msg = str(exc).lower()
            if attempt + 1 < _RETRY_ATTEMPTS and ("locked" in msg or "busy" in msg):
                time.sleep(_RETRY_BASE_DELAY_S * (2**attempt))
                continue
            raise
    return []  # unreachable: the final attempt either returns or raises


# --------------------------------------------------------------------------- rendering


def _json_list(raw: str | None) -> list[str]:
    """A claude-mem JSON-array column as list[str]; malformed content stays visible, NULL -> []."""
    if not raw:
        return []
    try:
        value = json.loads(raw)
    except ValueError:
        return [str(raw)]  # surface a malformed cell rather than drop it
    if isinstance(value, list):
        return [str(v) for v in value if v is not None and str(v)]
    return [str(value)]


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _iso_date(created_at: str) -> str:
    """The date portion of an ISO-8601 ``created_at`` (``2026-01-01T..`` -> ``2026-01-01``)."""
    return created_at.split("T", 1)[0]


def _clean(value: str | None) -> str:
    return (value or "").strip()


def render_observation(obs: Observation) -> str:
    """Render one observation row as a provenance-stamped doc, skipping every empty field."""
    lines = [f"[cm-obs-{obs.id} project={obs.project} {_iso_date(obs.created_at)}]"]
    title = _clean(obs.title)
    if title:
        lines.append(title)
    body = _clean(obs.narrative) or _clean(obs.text)
    if body:
        lines.append(body)
    facts = _json_list(obs.facts)
    if facts:
        lines += [f"- {fact}" for fact in facts]
    concepts = _json_list(obs.concepts)
    if concepts:
        lines.append(f"Concepts: {', '.join(concepts)}")
    files = _dedup(_json_list(obs.files_read) + _json_list(obs.files_modified))
    if files:
        lines.append(f"Files: {', '.join(files)}")
    return "\n".join(lines)


_SUMMARY_SECTIONS = (
    ("request", "Request"),
    ("investigated", "Investigated"),
    ("learned", "Learned"),
    ("completed", "Completed"),
    ("next_steps", "Next steps"),
    ("notes", "Notes"),
)


def render_summary(summary: SessionSummary) -> str:
    """Render one session-summary row as a provenance-stamped doc, skipping every empty field."""
    lines = [f"[cm-sum-{summary.id} project={summary.project} {_iso_date(summary.created_at)}]"]
    for attr, label in _SUMMARY_SECTIONS:
        value = _clean(getattr(summary, attr))
        if value:
            lines.append(f"{label}: {value}")
    files = _dedup(_json_list(summary.files_read) + _json_list(summary.files_edited))
    if files:
        lines.append(f"Files: {', '.join(files)}")
    return "\n".join(lines)


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- public API


def read_observations(db: Path, project: str, watermark: int) -> list[MemDoc]:
    """Observations for ``project`` with ``id > watermark``, id-ordered, rendered to docs.

    Read-only (invariant I1): the engine is ``mode=ro``. The watermark is an input; this reader
    persists nothing.
    """
    engine = _read_only_engine(db)

    def _run() -> list[MemDoc]:
        with Session(engine) as session:
            stmt = (
                select(Observation)
                .where(col(Observation.id) > watermark, Observation.project == project)
                .order_by(col(Observation.id))
            )
            rows = session.exec(stmt).all()
        docs: list[MemDoc] = []
        for row in rows:
            text = render_observation(row)
            docs.append(MemDoc(kind="obs", source_id=row.id, content_hash=_content_hash(text), text=text))
        return docs

    try:
        return _with_retry(_run)
    finally:
        engine.dispose()


def read_summaries(db: Path, project: str, watermark: int) -> list[MemDoc]:
    """Session summaries for ``project`` with ``id > watermark``, id-ordered, rendered to docs.

    Read-only (invariant I1): the engine is ``mode=ro``. The watermark is an input; this reader
    persists nothing.
    """
    engine = _read_only_engine(db)

    def _run() -> list[MemDoc]:
        with Session(engine) as session:
            stmt = (
                select(SessionSummary)
                .where(col(SessionSummary.id) > watermark, SessionSummary.project == project)
                .order_by(col(SessionSummary.id))
            )
            rows = session.exec(stmt).all()
        docs: list[MemDoc] = []
        for row in rows:
            text = render_summary(row)
            docs.append(MemDoc(kind="sum", source_id=row.id, content_hash=_content_hash(text), text=text))
        return docs

    try:
        return _with_retry(_run)
    finally:
        engine.dispose()
