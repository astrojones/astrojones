"""migrate-claude-mem: schema-faithful fixture store, granularity matrix, ledger resume, cost gate."""

import json
import sqlite3
from pathlib import Path

import pytest
from repo_agent_harness import claude_mem_migrate, digest_providers, mem
from repo_agent_harness.cognee_client import CogneeClient
from repo_agent_harness.models import ClaudeMemMigrateResult, MemError
from tests.fake_cognee import FakeCognee

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _wired(fake: FakeCognee) -> CogneeClient:
    return CogneeClient(**fake.client_kwargs())


# --------------------------------------------------------------- fixture store
# DDL copied verbatim from the real ~/.claude-mem/claude-mem.db (sqlite_master, 2026-07-16);
# the FTS shadow tables are omitted because the migrator never reads them.

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

# 2026-03-27T00:01:45Z expressed in ms and (a later instant) in s — the real store is
# ms-resolution but the normalizer must accept both units.
MS_MARCH = 1_774_569_705_439
S_MARCH = 1_774_569_905  # seconds epoch, same day
MS_APRIL = 1_776_211_200_000
MS_JUNE = 1_780_358_400_000
S_JUNE = 1_780_358_500  # seconds epoch, same day


def _obs_row(sid, project, type_, title, epoch, files_read=()):
    return (
        sid,
        project,
        type_,
        title,
        json.dumps(["fact one", "fact two"]),
        f"narrative for {title}",
        json.dumps(["how-it-works"]),
        json.dumps(list(files_read)),
        json.dumps([]),
        "2026-01-01T00:00:00.000Z",
        epoch,
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A frozen claude-mem store: 8 observations / 2 projects / 3 sessions (s1 lacks no summary, s2 does)."""
    db = tmp_path / "claude-mem.db"
    con = sqlite3.connect(db)
    con.execute(_OBS_DDL)
    con.execute(_SUM_DDL)
    con.executemany(
        "INSERT INTO observations (memory_session_id, project, type, title, facts, narrative, concepts,"
        " files_read, files_modified, created_at, created_at_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            _obs_row("s1", "alpha", "discovery", "Alpha discovery one", MS_MARCH, files_read=("a.py",)),
            _obs_row("s1", "alpha", "bugfix", "Alpha bugfix", MS_MARCH + 100_000),
            _obs_row("s1", "alpha", "decision", "Alpha decision", S_MARCH),  # seconds-epoch row
            _obs_row("s2", "alpha", "feature", "Alpha feature", MS_APRIL),
            _obs_row("s2", "alpha", "discovery", "Alpha discovery two", MS_APRIL + 100_000),
            _obs_row("s3", "beta", "change", "Beta change", MS_JUNE),
            _obs_row("s3", "beta", "refactor", "Beta refactor", MS_JUNE + 100_000),
            _obs_row("s3", "beta", "discovery", "Beta discovery", MS_JUNE + 200_000),
        ],
    )
    con.executemany(
        "INSERT INTO session_summaries (memory_session_id, project, request, investigated, learned,"
        " completed, next_steps, files_read, files_edited, notes, created_at, created_at_epoch)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        [
            (
                "s1",
                "alpha",
                "Fix the flux capacitor",
                "looked at wiring",
                "plutonium is required",
                "capacitor fluxing",
                "test at 88mph",
                json.dumps(["a.py"]),
                json.dumps([]),
                "session notes",
                "2026-03-27T02:06:56.136Z",
                1_774_577_216_136,
            ),
            ("s3", "beta", "Ship the beta", "", "", "shipped", "", None, None, None, "2026-06-01", S_JUNE),
        ],
    )
    con.commit()
    con.close()
    return db


# ------------------------------------------------------------ epoch normalizer


def test_normalize_epoch_boundary():
    """Values at/above the ms threshold divide by 1000; below stay seconds; None passes through."""
    assert claude_mem_migrate.normalize_epoch_s(None) is None
    assert claude_mem_migrate.normalize_epoch_s(1_774_569_705) == 1_774_569_705
    assert claude_mem_migrate.normalize_epoch_s(1_774_569_705_439) == pytest.approx(1_774_569_705.439)
    assert claude_mem_migrate.normalize_epoch_s(99_999_999_999) == 99_999_999_999
    assert claude_mem_migrate.normalize_epoch_s(100_000_000_000) == pytest.approx(100_000_000.0)


def test_iso_utc_renders_both_units_identically():
    assert claude_mem_migrate._iso_utc(1_774_569_705_000) == "2026-03-27T00:01:45Z"
    assert claude_mem_migrate._iso_utc(1_774_569_705) == "2026-03-27T00:01:45Z"
    assert claude_mem_migrate._iso_utc(None) == "unknown"


# ------------------------------------------------------------------ the ledger


def test_ledger_roundtrip_appends_batchwise(tmp_path):
    path = tmp_path / "ledger.json"
    assert claude_mem_migrate.ledger_read(path) == set()
    claude_mem_migrate.ledger_append(path, ["h1", "h2"])
    claude_mem_migrate.ledger_append(path, ["h3"])
    assert claude_mem_migrate.ledger_read(path) == {"h1", "h2", "h3"}


def test_ledger_survives_a_corrupt_line(tmp_path):
    path = tmp_path / "ledger.json"
    claude_mem_migrate.ledger_append(path, ["h1"])
    with path.open("a", encoding="utf-8") as f:
        f.write("{torn write, not json\n")
    claude_mem_migrate.ledger_append(path, ["h2"])
    assert claude_mem_migrate.ledger_read(path) == {"h1", "h2"}


# --------------------------------------------------------------------- dry run


async def test_dry_run_default_reports_without_cognee_calls(store):
    fake = FakeCognee()
    out = await claude_mem_migrate.migrate(store, "ds", client=_wired(fake))
    assert isinstance(out, ClaudeMemMigrateResult)
    assert out.dry_run is True
    assert out.granularity == "summaries-only"
    assert out.summaries == 2
    assert out.sessions == 2
    assert out.estimated_docs == 2
    assert out.skipped_dedup == 0
    assert out.estimate is not None
    assert out.estimate.estimated_cost_usd >= 0
    assert out.node_set == [mem.NODE_SET_CLAUDE_MEM_IMPORT]
    assert fake.requests == []  # a dry run never touches cognee


async def test_dry_run_granularity_matrix(store):
    raw = await claude_mem_migrate.migrate(store, "ds", granularity="raw")
    assert not isinstance(raw, MemError)
    assert raw.observations == 8
    assert raw.estimated_docs == 8  # raw: one doc per observation
    assert raw.per_project == {"alpha": 5, "beta": 3}
    assert raw.per_type == {"discovery": 3, "bugfix": 1, "decision": 1, "feature": 1, "change": 1, "refactor": 1}
    digest = await claude_mem_migrate.migrate(store, "ds", granularity="digest")
    assert not isinstance(digest, MemError)
    assert digest.sessions == 3
    assert digest.estimated_docs == 3  # estimate basis: one fallback doc per session, no LLM
    summaries = await claude_mem_migrate.migrate(store, "ds", granularity="summaries-only")
    assert not isinstance(summaries, MemError)
    assert summaries.estimated_docs == 2
    assert summaries.per_type == {"session_summary": 2}


async def test_unknown_granularity_is_a_memerror(store):
    out = await claude_mem_migrate.migrate(store, "ds", granularity="everything")
    assert isinstance(out, MemError)
    assert "granularity" in out.error


# ---------------------------------------------------------------- doc mapping


def test_render_observation_doc_shape():
    row = {
        "id": 7,
        "memory_session_id": "s1",
        "project": "alpha",
        "type": "discovery",
        "title": "Alpha discovery one",
        "subtitle": "a subtitle",
        "facts": json.dumps(["fact one", "fact two"]),
        "narrative": "the narrative",
        "concepts": json.dumps(["how-it-works"]),
        "files_read": json.dumps(["a.py"]),
        "files_modified": json.dumps(["b.py"]),
        "created_at_epoch": 1_774_569_705_439,
    }
    doc = claude_mem_migrate._render_observation(row)
    assert doc.startswith("# claude-mem discovery: Alpha discovery one")
    assert "Project: alpha" in doc
    assert "Observed: 2026-03-27T00:01:45Z" in doc
    assert "- fact one" in doc
    assert "- fact two" in doc
    assert "the narrative" in doc
    assert "Concepts: how-it-works" in doc
    assert "Files: a.py, b.py" in doc
    assert "[claude-mem: source=observation id=7 session=s1]" in doc


def test_render_summary_doc_shape():
    row = {
        "id": 3,
        "memory_session_id": "s1",
        "project": "alpha",
        "request": "Fix the flux capacitor",
        "investigated": "looked at wiring",
        "learned": "plutonium is required",
        "completed": "capacitor fluxing",
        "next_steps": "test at 88mph",
        "files_read": json.dumps(["a.py"]),
        "files_edited": None,
        "notes": "session notes",
        "created_at_epoch": 1_774_577_216_136,
    }
    doc = claude_mem_migrate._render_summary(row)
    assert doc.startswith("# claude-mem session summary: Fix the flux capacitor")
    assert "Project: alpha" in doc
    assert "Observed: 2026-03-27T02:06:56Z" in doc
    assert "Learned: plutonium is required" in doc
    assert "Next steps: test at 88mph" in doc
    assert "Files: a.py" in doc
    assert "[claude-mem: source=session_summary id=3 session=s1]" in doc


# -------------------------------------------------------------------- filters


async def test_filters_narrow_the_selection(store):
    by_project = await claude_mem_migrate.migrate(store, "ds", granularity="raw", projects=["alpha"])
    assert not isinstance(by_project, MemError)
    assert by_project.observations == 5
    assert by_project.per_project == {"alpha": 5}
    by_type = await claude_mem_migrate.migrate(store, "ds", granularity="raw", types=["discovery"])
    assert not isinstance(by_type, MemError)
    assert by_type.per_type == {"discovery": 3}
    since = await claude_mem_migrate.migrate(store, "ds", granularity="raw", since="2026-05-01")
    assert not isinstance(since, MemError)
    assert since.observations == 3  # only the June (beta) session
    until = await claude_mem_migrate.migrate(store, "ds", granularity="raw", until="2026-04-01")
    assert not isinstance(until, MemError)
    assert until.observations == 3  # only the March session, incl. its seconds-epoch row


async def test_bad_since_is_a_memerror(store):
    out = await claude_mem_migrate.migrate(store, "ds", since="not-a-date")
    assert isinstance(out, MemError)


# ------------------------------------------------------------------ bad stores


async def test_corrupt_db_is_a_memerror(tmp_path):
    db = tmp_path / "claude-mem.db"
    db.write_bytes(b"this is definitely not a sqlite file" * 64)
    out = await claude_mem_migrate.migrate(db, "ds")
    assert isinstance(out, MemError)
    assert out.hint is not None


async def test_missing_db_is_a_memerror(tmp_path):
    out = await claude_mem_migrate.migrate(tmp_path / "nope.db", "ds")
    assert isinstance(out, MemError)


# ------------------------------------------------------------------- execution


def _adds(fake: FakeCognee) -> list[dict]:
    return [payload for method, path, payload in fake.requests if path == "/api/v1/add"]


async def test_execution_requires_confirm(store):
    out = await claude_mem_migrate.migrate(store, "ds", granularity="raw", dry_run=False, confirm=False)
    assert isinstance(out, MemError)
    assert "confirm" in out.error


async def test_raw_execution_ships_per_project_batches_readonly(store):
    fake = FakeCognee(datasets=["ds"])
    before = store.read_bytes()
    out = await claude_mem_migrate.migrate(
        store, "ds", node_set=["extra"], granularity="raw", dry_run=False, confirm=True, client=_wired(fake)
    )
    assert not isinstance(out, MemError)
    assert out.dry_run is False
    assert out.shipped == 8
    assert out.per_project == {"alpha": 5, "beta": 3}
    adds = _adds(fake)
    node_sets = {tuple(p["node_set"]) for p in adds}
    assert (mem.NODE_SET_CLAUDE_MEM_IMPORT, f"{mem.PROJECT_TAG_PREFIX}alpha", "extra") in node_sets
    assert (mem.NODE_SET_CLAUDE_MEM_IMPORT, f"{mem.PROJECT_TAG_PREFIX}beta", "extra") in node_sets
    assert store.read_bytes() == before  # read-only guarantee: the source store is untouched


async def test_second_run_resumes_and_ships_zero(store):
    fake = FakeCognee(datasets=["ds"])
    first = await claude_mem_migrate.migrate(
        store, "ds", granularity="raw", dry_run=False, confirm=True, client=_wired(fake)
    )
    assert not isinstance(first, MemError)
    assert first.shipped == 8
    # a torn ledger line must not break the resume
    with claude_mem_migrate.ledger_path("ds").open("a", encoding="utf-8") as f:
        f.write("{torn line\n")
    fake2 = FakeCognee(datasets=["ds"])
    second = await claude_mem_migrate.migrate(
        store, "ds", granularity="raw", dry_run=False, confirm=True, client=_wired(fake2)
    )
    assert not isinstance(second, MemError)
    assert second.shipped == 0
    assert second.skipped_dedup == 8
    assert _adds(fake2) == []


async def test_cost_gate_refuses_before_any_batch(store, monkeypatch):
    monkeypatch.setenv("COGNEE_INGEST_COST_LIMIT_USD", "0.0000001")
    fake = FakeCognee(datasets=["ds"])
    out = await claude_mem_migrate.migrate(
        store, "ds", granularity="raw", dry_run=False, confirm=True, client=_wired(fake)
    )
    assert isinstance(out, MemError)
    assert out.estimate is not None
    assert fake.requests == []  # refused centrally, before any batch shipped


# ------------------------------------------------------------------ digesting


async def test_digest_provider_off_falls_back_per_session(store, monkeypatch):
    monkeypatch.setenv(digest_providers.PROVIDER_ENV, "off")
    fake = FakeCognee(datasets=["ds"])
    out = await claude_mem_migrate.migrate(
        store, "ds", granularity="digest", dry_run=False, confirm=True, client=_wired(fake)
    )
    assert not isinstance(out, MemError)
    assert out.shipped == 3  # one fallback doc per session — no session silently dropped
    data = "\n".join(str(p["data"]) for p in _adds(fake))
    assert "Fix the flux capacitor" in data  # s1: session summary is the fallback seed
    assert "Alpha feature" in data  # s2 has no summary -> bounded titles+facts concat


async def test_digest_observations_render_typed_docs(store, monkeypatch):
    async def fake_digest(entries):
        assert entries  # the group's rendered rows are the digest input
        return digest_providers.DigestResult(
            observations=[
                digest_providers.DigestObservation(
                    type="decision", title="Distilled", facts=["f1"], concepts=["how-it-works"], files=["a.py"]
                )
            ]
        )

    monkeypatch.setattr(digest_providers, "digest", fake_digest)
    fake = FakeCognee(datasets=["ds"])
    out = await claude_mem_migrate.migrate(
        store, "ds", granularity="digest", dry_run=False, confirm=True, client=_wired(fake)
    )
    assert not isinstance(out, MemError)
    assert out.shipped == 3  # one distilled observation per session
    data = "\n".join(str(p["data"]) for p in _adds(fake))
    assert "# claude-mem decision: Distilled" in data


async def test_digest_text_reply_ships_one_doc_per_session(store, monkeypatch):
    async def fake_digest(entries):
        return digest_providers.DigestResult(text="prose digest of the session")

    monkeypatch.setattr(digest_providers, "digest", fake_digest)
    fake = FakeCognee(datasets=["ds"])
    out = await claude_mem_migrate.migrate(
        store, "ds", granularity="digest", dry_run=False, confirm=True, client=_wired(fake)
    )
    assert not isinstance(out, MemError)
    assert out.shipped == 3
    data = "\n".join(str(p["data"]) for p in _adds(fake))
    assert "prose digest of the session" in data


async def test_summaries_only_never_calls_the_digest_llm(store, monkeypatch):
    def boom(entries):
        msg = "summaries-only must make zero LLM calls"
        raise AssertionError(msg)

    monkeypatch.setattr(digest_providers, "digest", boom)
    fake = FakeCognee(datasets=["ds"])
    out = await claude_mem_migrate.migrate(
        store, "ds", granularity="summaries-only", dry_run=False, confirm=True, client=_wired(fake)
    )
    assert not isinstance(out, MemError)
    assert out.shipped == 2
    data = "\n".join(str(p["data"]) for p in _adds(fake))
    assert "Fix the flux capacitor" in data
    assert "Ship the beta" in data


# ---------------------------------------------------------------- CLI wiring


def test_cli_migrate_claude_mem_dry_run(repo, monkeypatch, capsys, store):
    """The subcommand defaults to a dry-run report — no cognee config needed at all."""
    from repo_agent_harness import cli

    monkeypatch.chdir(repo)
    code = cli.main(["migrate-claude-mem", "--db", str(store), "--dataset", "ds"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["dry_run"] is True
    assert out["granularity"] == "summaries-only"
    assert out["estimated_docs"] == 2
    assert out["node_set"] == [mem.NODE_SET_CLAUDE_MEM_IMPORT]


def test_cli_migrate_claude_mem_passes_filters_and_confirm(repo, monkeypatch, capsys, store):
    from repo_agent_harness import cli

    seen = {}

    async def fake_migrate(db_path, dataset, **kwargs):
        seen.update(kwargs, db_path=db_path, dataset=dataset)
        return ClaudeMemMigrateResult(dataset=dataset, granularity=kwargs["granularity"], db=str(db_path))

    monkeypatch.setattr(claude_mem_migrate, "migrate", fake_migrate)
    monkeypatch.chdir(repo)
    code = cli.main(
        [
            "migrate-claude-mem",
            "--db",
            str(store),
            "--dataset",
            "ds",
            "--granularity",
            "raw",
            "--project",
            "alpha",
            "--types",
            "discovery",
            "--node-set",
            "extra",
            "--since",
            "2026-05-01",
            "--confirm",
        ]
    )
    assert code == 0
    assert seen["dataset"] == "ds"
    assert seen["granularity"] == "raw"
    assert seen["projects"] == ["alpha"]
    assert seen["types"] == ["discovery"]
    assert seen["node_set"] == ["extra"]
    assert seen["since"] == "2026-05-01"
    assert seen["dry_run"] is False
    assert seen["confirm"] is True


def test_cli_memify_subcommand(repo, monkeypatch, capsys):
    from repo_agent_harness import cli

    calls = []

    async def fake_run_memify(root, dataset, client=None):
        calls.append((root, dataset))
        return True

    monkeypatch.setattr(mem, "run_memify", fake_run_memify)
    monkeypatch.chdir(repo)
    code = cli.main(["memify", "--dataset", "ds", "--node-name", "n1"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"] is True
    assert out["dataset"] == "ds"
    assert out["node_name"] == "n1"
    assert len(calls) == 1
    assert calls[0][1] == "ds"
