"""Read-only claude-mem reader + our writable sync ledger.

The fixture store is built from the EXACT base DDL of the real ~/.claude-mem/claude-mem.db
(the FTS virtual/shadow tables and AI/AD/AU triggers are dropped — they are never read and their
insert-time triggers are irrelevant to a read path). Key regression: a row with every nullable
column NULL must render without a Pydantic ValidationError.
"""

import sqlite3
from pathlib import Path

import pytest
from repo_agent_harness import claude_mem_reader, paths
from repo_agent_harness.sync_ledger import SyncLedger
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

# Base CREATE TABLE statements dumped read-only from the live store (triggers + FTS dropped).
_OBS_DDL = """
CREATE TABLE "observations" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  text TEXT,
  type TEXT NOT NULL,
  title TEXT,
  subtitle TEXT,
  facts TEXT,
  narrative TEXT,
  concepts TEXT,
  files_read TEXT,
  files_modified TEXT,
  prompt_number INTEGER,
  discovery_tokens INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  content_hash TEXT, generated_by_model TEXT, relevance_count INTEGER DEFAULT 0,
  merged_into_project TEXT, agent_type TEXT, agent_id TEXT, metadata TEXT
)
"""

_SUM_DDL = """
CREATE TABLE "session_summaries" (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  memory_session_id TEXT NOT NULL,
  project TEXT NOT NULL,
  request TEXT,
  investigated TEXT,
  learned TEXT,
  completed TEXT,
  next_steps TEXT,
  files_read TEXT,
  files_edited TEXT,
  notes TEXT,
  prompt_number INTEGER,
  discovery_tokens INTEGER DEFAULT 0,
  created_at TEXT NOT NULL,
  created_at_epoch INTEGER NOT NULL,
  merged_into_project TEXT
)
"""


def _store(tmp_path: Path) -> Path:
    db = tmp_path / "claude-mem.db"
    con = sqlite3.connect(db)
    con.execute(_OBS_DDL)
    con.execute(_SUM_DDL)
    con.commit()
    con.close()
    return db


def _insert_obs(db: Path, **cols) -> None:
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    con = sqlite3.connect(db)
    con.execute(f"INSERT INTO observations ({keys}) VALUES ({marks})", tuple(cols.values()))
    con.commit()
    con.close()


def _insert_sum(db: Path, **cols) -> None:
    keys = ", ".join(cols)
    marks = ", ".join("?" for _ in cols)
    con = sqlite3.connect(db)
    con.execute(f"INSERT INTO session_summaries ({keys}) VALUES ({marks})", tuple(cols.values()))
    con.commit()
    con.close()


_NOT_NULL_OBS = {
    "memory_session_id": "s1",
    "project": "alpha",
    "type": "discovery",
    "created_at": "2026-01-01T00:00:00.000Z",
    "created_at_epoch": 1_767_225_600_000,
}
_NOT_NULL_SUM = {
    "memory_session_id": "s1",
    "project": "alpha",
    "created_at": "2026-01-01T00:00:00.000Z",
    "created_at_epoch": 1_767_225_600_000,
}


# --------------------------------------------------------------------------- nullability regression


def test_all_null_observation_renders_cleanly(tmp_path):
    """Every nullable column NULL (incl. text/title) must render without a ValidationError."""
    db = _store(tmp_path)
    _insert_obs(db, **_NOT_NULL_OBS)  # only NOT NULL columns; everything nullable stays NULL
    docs = claude_mem_reader.read_observations(db, "alpha", 0)
    assert len(docs) == 1
    assert docs[0].kind == "obs"
    assert docs[0].source_id == 1
    assert docs[0].text == "[cm-obs-1 project=alpha 2026-01-01]"  # header only, no body fields
    assert docs[0].content_hash  # a stable render-hash is still produced


def test_all_null_summary_renders_cleanly(tmp_path):
    db = _store(tmp_path)
    _insert_sum(db, **_NOT_NULL_SUM)
    docs = claude_mem_reader.read_summaries(db, "alpha", 0)
    assert len(docs) == 1
    assert docs[0].kind == "sum"
    assert docs[0].text == "[cm-sum-1 project=alpha 2026-01-01]"


def test_json_array_null_element_does_not_leak_none(tmp_path):
    """A null element inside an otherwise-valid JSON array must not render the literal 'None'."""
    db = _store(tmp_path)
    _insert_obs(db, **_NOT_NULL_OBS, facts='["real fact", null, ""]')
    docs = claude_mem_reader.read_observations(db, "alpha", 0)
    assert "real fact" in docs[0].text
    assert "None" not in docs[0].text


def test_observation_renders_all_meaningful_fields(tmp_path):
    db = _store(tmp_path)
    _insert_obs(
        db,
        **_NOT_NULL_OBS,
        title="Alpha discovery",
        narrative="the narrative body",
        facts='["fact one", "fact two"]',
        concepts='["how-it-works"]',
        files_read='["a.py"]',
        files_modified='["b.py"]',
    )
    text_doc = claude_mem_reader.read_observations(db, "alpha", 0)[0].text
    assert text_doc.startswith("[cm-obs-1 project=alpha 2026-01-01]")
    assert "Alpha discovery" in text_doc
    assert "the narrative body" in text_doc
    assert "- fact one" in text_doc
    assert "Concepts: how-it-works" in text_doc
    assert "Files: a.py, b.py" in text_doc


def test_summary_renders_sections(tmp_path):
    db = _store(tmp_path)
    _insert_sum(
        db,
        **_NOT_NULL_SUM,
        request="Fix the flux capacitor",
        learned="plutonium is required",
        next_steps="test at 88mph",
        files_read='["a.py"]',
    )
    text_doc = claude_mem_reader.read_summaries(db, "alpha", 0)[0].text
    assert text_doc.startswith("[cm-sum-1 project=alpha 2026-01-01]")
    assert "Request: Fix the flux capacitor" in text_doc
    assert "Learned: plutonium is required" in text_doc
    assert "Next steps: test at 88mph" in text_doc
    assert "Files: a.py" in text_doc


# --------------------------------------------------------------------------- watermark + project scope


def test_watermark_and_project_scoping(tmp_path):
    db = _store(tmp_path)
    # ids 1..4 land by insertion order: alpha, beta, alpha, alpha
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "alpha", "title": "a1"})  # id 1
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "beta", "title": "b1"})  # id 2
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "alpha", "title": "a2"})  # id 3
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "alpha", "title": "a3"})  # id 4

    all_alpha = claude_mem_reader.read_observations(db, "alpha", 0)
    assert [d.source_id for d in all_alpha] == [1, 3, 4]  # beta excluded, id order preserved

    past_1 = claude_mem_reader.read_observations(db, "alpha", 1)
    assert [d.source_id for d in past_1] == [3, 4]  # id > watermark only

    assert [d.source_id for d in claude_mem_reader.read_observations(db, "beta", 0)] == [2]
    assert claude_mem_reader.read_observations(db, "alpha", 4) == []  # nothing past the tail


# --------------------------------------------------------------------------- read-only guarantee


def test_reader_engine_rejects_writes(tmp_path):
    """The reader's engine must REJECT writes (invariant I1), not merely avoid them."""
    db = _store(tmp_path)
    _insert_obs(db, **_NOT_NULL_OBS)
    engine = claude_mem_reader._read_only_engine(db)
    try:
        with pytest.raises(OperationalError, match="readonly"), engine.begin() as conn:
            conn.execute(text("UPDATE observations SET title = 'tampered' WHERE id = 1"))
    finally:
        engine.dispose()


def test_reader_source_has_no_write_or_ddl_calls():
    """I2-style source scan: the read-only module never emits a write, PRAGMA, or DDL call."""
    src = Path(claude_mem_reader.__file__).read_text(encoding="utf-8")
    # Call-shaped tokens: a real write/DDL/PRAGMA-execution would surface as one of these; prose
    # mentions (e.g. "not a PRAGMA") stay clear of the call syntax deliberately.
    forbidden = (
        "create_all",
        "exec_driver_sql",
        "journal_mode",
        "session.add",
        ".commit(",
        "INSERT INTO",
        "UPDATE ",
        "DELETE FROM",
        "DROP TABLE",
    )
    for token in forbidden:
        assert token not in src, f"read-only reader must not contain {token!r}"


# --------------------------------------------------------------------------- sync ledger


def test_ledger_creates_its_own_db_under_sync_ledger_file():
    assert not paths.sync_ledger_file().exists()
    ledger = SyncLedger()
    assert ledger is not None
    assert paths.sync_ledger_file().exists()


def test_ledger_watermark_only_advances_on_ok_rows():
    ledger = SyncLedger()
    assert ledger.watermark("obs") == 0  # empty
    ledger.record("obs", 5, "h5", "ds")
    ledger.record("obs", 3, "h3", "ds")  # lower id, still ok
    assert ledger.watermark("obs") == 5
    ledger.record("obs", 10, "h10", "ds", verify_status="failed")  # higher id but NOT ok
    assert ledger.watermark("obs") == 5  # a failed ship must not advance the watermark
    assert ledger.watermark("sum") == 0  # kinds are independent


def test_ledger_already_ok_dedup():
    ledger = SyncLedger()
    assert ledger.already_ok("h1") is False
    ledger.record("sum", 1, "h1", "ds")
    assert ledger.already_ok("h1") is True
    ledger.record("sum", 2, "h2", "ds", verify_status="failed")
    assert ledger.already_ok("h2") is False  # a failed row is not a dedup hit
