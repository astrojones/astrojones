"""mem_* business logic: contracts, cost pre-flight, serial-first cognify, doctor sentinels."""

import json
import os
import time

import pytest
from repo_agent_harness import mem, paths
from repo_agent_harness.cognee_client import CogneeClient, CogneeError
from repo_agent_harness.models import (
    MemError,
    MemIngestIn,
    MemIngestResult,
    MemOntologyIn,
    MemRememberIn,
    MemSearchIn,
    MemSearchResult,
    MemStatsIn,
    MemStatsResult,
)
from tests.fake_cognee import FakeCognee

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _wired(fake: FakeCognee) -> CogneeClient:
    return CogneeClient(**fake.client_kwargs())


def _unconfigured() -> CogneeClient:
    return CogneeClient(url=None, auth=None, key=None)


# ------------------------------------------------------------------------- search


async def test_search_returns_shaped_result():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.search(MemSearchIn(query="what happened?", dataset="kolbe"), client=_wired(fake))
    assert isinstance(out, MemSearchResult)
    assert out.results == [{"text": "canned:GRAPH_COMPLETION"}]
    assert out.search_type == "GRAPH_COMPLETION"
    assert out.dataset == "kolbe"
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["datasets"] == ["kolbe"]
    assert payload["topK"] == 10


async def test_search_forwards_node_name_to_client():
    """mem.search threads MemSearchIn.node_name into the cognee /search payload (nodeName)."""
    fake = FakeCognee(datasets=["kolbe"])
    await mem.search(
        MemSearchIn(query="q", search_type="CHUNKS", dataset="kolbe", node_name=["session_digest"]),
        client=_wired(fake),
    )
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["nodeName"] == ["session_digest"]


async def test_search_scopes_to_onboarded_dataset_by_default(tmp_path):
    """dataset=None + a root resolves to the repo's onboarded dataset — reads match writes."""
    root = tmp_path / "repo"
    root.mkdir()
    paths.mark_cognee_onboarded(str(root), dataset="projX")
    fake = FakeCognee(datasets=["projX"])
    out = await mem.search(MemSearchIn(query="q", dataset=None), client=_wired(fake), root=str(root))
    assert isinstance(out, MemSearchResult)
    assert out.dataset == "projX"
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["datasets"] == ["projX"]


async def test_search_scopes_to_default_without_root():
    """No root, no dataset -> the shared default scope (not span-all) — the unified default."""
    fake = FakeCognee()
    await mem.search(MemSearchIn(query="q"), client=_wired(fake))
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["datasets"] == ["agent_sessions"]


async def test_ingest_resolves_none_dataset_via_root(tmp_path):
    """mem_ingest with dataset=None resolves to the onboarded dataset, unified with writes."""
    root = tmp_path / "repo"
    root.mkdir()
    paths.mark_cognee_onboarded(str(root), dataset="projX")
    fake = FakeCognee(datasets=["projX"])
    out = await mem.ingest(MemIngestIn(items=["doc"], dataset=None, confirm=True), client=_wired(fake), root=str(root))
    assert not isinstance(out, MemError)
    assert out.dataset == "projX"
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["datasetName"] == "projX"


def test_search_input_rejects_unknown_type():
    """The search_type vocabulary is enforced by the input model, before any network."""
    with pytest.raises(ValueError, match="search_type"):
        MemSearchIn(query="q", search_type="FEELING_LUCKY")  # ty: ignore[invalid-argument-type] - the runtime rejection IS the test


async def test_rules_is_a_coding_rules_search():
    fake = FakeCognee()
    out = await mem.rules("python error handling", client=_wired(fake))
    assert isinstance(out, MemSearchResult)
    assert out.search_type == "CODING_RULES"
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["searchType"] == "CODING_RULES"
    # No root/dataset -> resolves to the shared default scope (reads agree with writes).
    assert payload["datasets"] == ["agent_sessions"]


async def test_rules_scopes_to_dataset_when_given():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.rules("q", dataset="kolbe", client=_wired(fake))
    assert isinstance(out, MemSearchResult)
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["datasets"] == ["kolbe"]


async def test_search_maps_client_failure_to_mem_error():
    out = await mem.search(MemSearchIn(query="q"), client=_unconfigured())
    assert isinstance(out, MemError)
    assert "cognee not configured" in out.error
    assert "COGNEE_BASE_URL" in (out.hint or "")


# ----------------------------------------------------------------------- remember


async def test_remember_adds_then_background_cognifies():
    fake = FakeCognee(datasets=["agent_sessions"])
    out = await mem.remember(MemRememberIn(text="fact", node_set=["project_docs"]), client=_wired(fake))
    assert not isinstance(out, MemError)
    assert out.queued is True
    assert out.dataset == "agent_sessions"
    assert out.add_id
    calls = [(path, p) for m, path, p in fake.requests if path in {"/api/v1/add", "/api/v1/cognify"}]
    assert [path for path, _ in calls] == ["/api/v1/add", "/api/v1/cognify"]
    add_payload = calls[0][1]
    assert add_payload["datasetName"] == "agent_sessions"
    assert add_payload["node_set"] == "project_docs"
    cognify_payload = calls[1][1]
    assert cognify_payload["runInBackground"] is True


async def test_remember_folds_metadata_into_text():
    fake = FakeCognee(datasets=["agent_sessions"])
    await mem.remember(MemRememberIn(text="fact", metadata={"repo": "astrojones"}), client=_wired(fake))
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert "repo=astrojones" in add_payload["data"]


async def test_remember_resolves_none_dataset_via_conventions(tmp_path):
    """dataset=None resolves through resolve_dataset: onboarded marker wins, else the default."""
    root = tmp_path / "repo"
    root.mkdir()
    paths.mark_cognee_onboarded(str(root), dataset="kolbe_docs")
    fake = FakeCognee(datasets=["kolbe_docs"])
    out = await mem.remember(MemRememberIn(text="fact", dataset=None), client=_wired(fake), root=str(root))
    assert not isinstance(out, MemError)
    assert out.dataset == "kolbe_docs"
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["datasetName"] == "kolbe_docs"
    # An explicit dataset always wins over the marker.
    out = await mem.remember(MemRememberIn(text="fact", dataset="explicit"), client=_wired(fake), root=str(root))
    assert not isinstance(out, MemError)
    assert out.dataset == "explicit"
    # No root context -> the shared default scope, exactly the old behavior.
    fake2 = FakeCognee(datasets=["agent_sessions"])
    out = await mem.remember(MemRememberIn(text="fact", dataset=None), client=_wired(fake2))
    assert not isinstance(out, MemError)
    assert out.dataset == "agent_sessions"


# ------------------------------------------------------------------------- ingest


async def test_ingest_dry_run_estimates_without_writing():
    fake = FakeCognee()
    out = await mem.ingest(MemIngestIn(items=["x" * 4000], dataset="docs", dry_run=True), client=_wired(fake))
    assert isinstance(out, MemIngestResult)
    assert out.dry_run is True
    assert out.estimate.estimated_tokens == 1000
    assert out.estimate.estimated_cost_usd > 0
    assert fake.requests == []


async def test_ingest_refuses_expensive_run_without_confirm(monkeypatch):
    monkeypatch.setenv("COGNEE_INGEST_COST_LIMIT_USD", "0.001")
    fake = FakeCognee()
    out = await mem.ingest(MemIngestIn(items=["x" * 400_000], dataset="docs"), client=_wired(fake))
    assert isinstance(out, MemError)
    assert "ingest refused" in out.error
    assert "confirm=true" in (out.hint or "")
    assert out.estimate is not None
    assert fake.requests == []


async def test_ingest_fresh_dataset_is_serial_first():
    """On a fresh dataset: awaited single-doc cognify (batch=1) BEFORE the bulk background one."""
    fake = FakeCognee(datasets=["other"])
    inp = MemIngestIn(items=["doc-a", "doc-b", "doc-c"], dataset="fresh_ds", confirm=True)
    out = await mem.ingest(inp, client=_wired(fake))
    assert isinstance(out, MemIngestResult)
    assert out.fresh_dataset is True
    assert out.serial_first is True
    assert out.ingested == 3
    ordered = [(path, p) for m, path, p in fake.requests if path in {"/api/v1/add", "/api/v1/cognify"}]
    assert [path for path, _ in ordered] == [
        "/api/v1/add",  # first doc alone
        "/api/v1/cognify",  # awaited, dataPerBatch=1
        "/api/v1/add",  # the rest
        "/api/v1/cognify",  # background
    ]
    first_cognify = ordered[1][1]
    assert first_cognify["runInBackground"] is False
    assert first_cognify["dataPerBatch"] == 1
    assert first_cognify["chunksPerBatch"] == 1
    second_cognify = ordered[3][1]
    assert second_cognify["runInBackground"] is True


async def test_ingest_existing_dataset_skips_serial_first():
    fake = FakeCognee(datasets=["docs"])
    out = await mem.ingest(MemIngestIn(items=["a", "b"], dataset="docs"), client=_wired(fake))
    assert isinstance(out, MemIngestResult)
    assert out.fresh_dataset is False
    cognifies = [p for m, path, p in fake.requests if path == "/api/v1/cognify"]
    assert len(cognifies) == 1
    assert cognifies[0]["runInBackground"] is True


async def test_ingest_binds_pinned_ontology_key_to_cognify():
    """A non-dry-run ingest threads ontology_key into every cognify payload as ``ontology_key`` (a list)."""
    fake = FakeCognee(datasets=["other"])
    inp = MemIngestIn(
        items=["doc-a", "doc-b"],
        dataset="fresh_ds",
        ontology_key="harness-abc",
        confirm=True,
    )
    out = await mem.ingest(inp, client=_wired(fake))
    assert isinstance(out, MemIngestResult)
    cognifies = [p for m, path, p in fake.requests if path == "/api/v1/cognify"]
    assert cognifies
    assert all(p["ontology_key"] == ["harness-abc"] for p in cognifies)


async def test_ingest_without_ontology_key_omits_it():
    """Omitting ontology_key leaves no ``ontology_key`` in the cognify payload."""
    fake = FakeCognee(datasets=["docs"])
    out = await mem.ingest(MemIngestIn(items=["a", "b"], dataset="docs"), client=_wired(fake))
    assert isinstance(out, MemIngestResult)
    cognifies = [p for m, path, p in fake.requests if path == "/api/v1/cognify"]
    assert cognifies
    assert all("ontology_key" not in p and "ontologyKey" not in p for p in cognifies)


async def test_ontology_exists_checks_the_listing_for_membership(monkeypatch):
    """``ontology_exists`` lists ``GET /api/v1/ontologies`` (a dict) and checks key membership."""
    client = _wired(FakeCognee())
    calls: list[tuple[str, str]] = []

    async def fake_request(method, path, *, idempotent=False, **kwargs):
        calls.append((method, path))
        return {"harness-abc": {"description": "pinned"}}

    monkeypatch.setattr(client, "request", fake_request)
    assert await client.ontology_exists("harness-abc") is True
    assert await client.ontology_exists("missing") is False
    assert calls == [("GET", "/api/v1/ontologies"), ("GET", "/api/v1/ontologies")]


async def test_ontology_exists_is_defensive_about_non_dict_listings(monkeypatch):
    """A non-dict/empty listing response means the key is simply absent, not an error."""
    client = _wired(FakeCognee())

    async def empty_request(method, path, *, idempotent=False, **kwargs):
        return None

    monkeypatch.setattr(client, "request", empty_request)
    assert await client.ontology_exists("harness-abc") is False


async def test_ingest_empty_items_is_an_error():
    out = await mem.ingest(MemIngestIn(items=[], dataset="docs"), client=_unconfigured())
    assert isinstance(out, MemError)
    assert out.error == "no items to ingest"


# -------------------------------------------------------------------------- stats


async def test_stats_reports_existence_and_honest_unsupported_counts():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.stats(MemStatsIn(dataset="kolbe"), client=_wired(fake))
    assert isinstance(out, MemStatsResult)
    assert out.dataset_id == "id-kolbe"
    assert out.node_counts_supported is False
    status_call = next(p for m, path, p in fake.requests if path == "/api/v1/datasets/status")
    assert status_call["dataset"] == "id-kolbe"  # the API takes the dataset ID, not its name


async def test_stats_unknown_dataset_lists_available():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.stats(MemStatsIn(dataset="nope"), client=_wired(fake))
    assert isinstance(out, MemError)
    assert "not found" in out.error
    assert out.available == ["kolbe"]


# ----------------------------------------------------------------------- ontology


def test_ontology_document_is_named_individuals_only():
    xml = mem.ontology_document({"Zeugnis Raster": "Artifact", "kolbe-api": "Service"})
    assert xml.count("owl:NamedIndividual") == 4  # open+close per individual
    assert "owl:Class" not in xml
    assert "Zeugnis_Raster" in xml  # sanitized URI
    assert 'rdf:resource="http://repo-agent-harness/ontology#Artifact"' in xml


def test_ontology_prompt_pins_the_exact_type_set():
    prompt = mem.ontology_prompt({"a": "Service", "b": "Artifact", "c": "Service"})
    assert "EXACTLY ONE of: Artifact, Service" in prompt


async def test_ontology_uploads_once_then_is_idempotent():
    fake = FakeCognee()
    inp = MemOntologyIn(individuals={"kolbe-api": "Service", "Zeugnis": "Artifact"})
    first = await mem.ontology(inp, client=_wired(fake))
    assert not isinstance(first, MemError)
    assert first.uploaded is True
    assert first.types == ["Artifact", "Service"]
    second = await mem.ontology(inp, client=_wired(fake))
    assert not isinstance(second, MemError)
    assert second.uploaded is False
    assert second.ontology_key == first.ontology_key
    uploads = [p for m, path, p in fake.requests if path == "/api/v1/ontologies" and m == "POST"]
    assert len(uploads) == 1


async def test_ontology_empty_input_is_an_error():
    out = await mem.ontology(MemOntologyIn(individuals={}), client=_unconfigured())
    assert isinstance(out, MemError)
    assert "no individuals" in out.error


# ------------------------------------------------------------------------- doctor


async def test_doctor_green_against_live_fake():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.doctor(client=_wired(fake))
    assert out.configured is True
    assert out.reachable is True
    assert out.authenticated is True
    assert out.datasets == ["kolbe"]


async def test_doctor_unconfigured_hints_env_vars():
    out = await mem.doctor(client=_unconfigured())
    assert out.configured is False
    assert any("COGNEE_BASE_URL" in h for h in out.hints)


async def test_doctor_unreachable_server_is_reported_not_raised():
    fake = FakeCognee()
    fake.transport_failures = 99
    out = await mem.doctor(client=_wired(fake))
    assert out.reachable is False
    assert any("health probe failed" in h for h in out.hints)


async def test_doctor_detects_live_claude_mem_capture(tmp_path, monkeypatch):
    fake = FakeCognee(datasets=["kolbe"])
    db = tmp_path / ".claude-mem" / "claude-mem.db"
    db.parent.mkdir()
    db.write_text("x")
    monkeypatch.setattr(mem, "_CLAUDE_MEM_DB", db)
    out = await mem.doctor(client=_wired(fake))
    assert any("claude-mem capture looks LIVE" in h for h in out.hints)
    # An old, quiet DB is not flagged.
    old = time.time() - 3600
    os.utime(db, (old, old))
    out = await mem.doctor(client=_wired(fake))
    assert not any("claude-mem" in h for h in out.hints)


async def test_doctor_detects_pending_cognee_plugin_captures(tmp_path, monkeypatch):
    fake = FakeCognee(datasets=["kolbe"])
    pending = tmp_path / ".cognee-plugin" / "claude-code" / "pending"
    pending.mkdir(parents=True)
    (pending / "cap.json").write_text("{}")
    monkeypatch.setattr(mem, "_COGNEE_PLUGIN_DIR", tmp_path / ".cognee-plugin")
    out = await mem.doctor(client=_wired(fake))
    assert any("cognee-memory plugin capture looks LIVE" in h for h in out.hints)


async def test_doctor_flags_never_or_stale_stop_heartbeat(tmp_path):
    """Exactly one hint when session-start ran but stop never/last stamped before it."""
    root = tmp_path / "repo"
    root.mkdir()
    fake = FakeCognee(datasets=["kolbe"])
    hint = "stop hook heartbeat never/stale"

    def beat(event: str, ts: float) -> None:
        paths.hook_heartbeat_file(str(root), event).write_text(json.dumps({"ts": ts, "count": 1}))

    # No heartbeats at all: hooks may simply not be installed — not this hint's business.
    out = await mem.doctor(client=_wired(fake), root=str(root))
    assert not any(hint in h for h in out.hints)
    # session-start ran but stop NEVER did -> captures may never be enqueued.
    beat("session-start", 200.0)
    out = await mem.doctor(client=_wired(fake), root=str(root))
    assert sum(hint in h for h in out.hints) == 1
    # stop is STALE (older than the last session-start) -> the same single hint.
    beat("stop", 100.0)
    out = await mem.doctor(client=_wired(fake), root=str(root))
    assert sum(hint in h for h in out.hints) == 1
    # Fresh stop (after the last session-start) -> healthy, no hint.
    beat("stop", 300.0)
    out = await mem.doctor(client=_wired(fake), root=str(root))
    assert not any(hint in h for h in out.hints)
    # No root (today's server wiring) -> the check is silently skipped.
    out = await mem.doctor(client=_wired(fake))
    assert not any(hint in h for h in out.hints)
    # FIRST TURN of a new session: stop stamped seconds before this session-start
    # (healthy end of the previous session) — recency must veto the staleness hint.
    now = time.time()
    beat("stop", now - 60)
    beat("session-start", now)
    out = await mem.doctor(client=_wired(fake), root=str(root))
    assert not any(hint in h for h in out.hints)


async def test_doctor_heartbeat_hint_fires_without_cognee_configured(tmp_path):
    """A dead stop hook loses local captures regardless of cognee config — hint fires unconfigured too."""
    root = tmp_path / "repo"
    root.mkdir()
    paths.hook_heartbeat_file(str(root), "session-start").write_text(json.dumps({"ts": time.time(), "count": 3}))
    out = await mem.doctor(client=_unconfigured(), root=str(root))
    assert any("stop hook heartbeat never/stale" in h for h in out.hints)


async def test_doctor_hints_malformed_secrets_yml(tmp_path, isolated_harness_home):
    """A malformed repo secrets.yml used to drop capture rows silently — doctor must surface it."""
    root = tmp_path / "repo"
    root.mkdir()
    pol = isolated_harness_home / "repos" / paths.repo_id(str(root)) / "policies"
    pol.mkdir(parents=True)
    (pol / "secrets.yml").write_text('redact_patterns:\n  - "foo("\n')
    out = await mem.doctor(client=_unconfigured(), root=str(root))
    assert any("invalid redact pattern" in h for h in out.hints)


async def test_doctor_hints_malformed_secrets_yml_when_configured(tmp_path, isolated_harness_home):
    """The secrets hint fires on the configured branch too — both doctor paths are wired."""
    root = tmp_path / "repo"
    root.mkdir()
    pol = isolated_harness_home / "repos" / paths.repo_id(str(root)) / "policies"
    pol.mkdir(parents=True)
    (pol / "secrets.yml").write_text('redact_patterns:\n  - "foo("\n')
    fake = FakeCognee(datasets=["agent_sessions"])
    out = await mem.doctor(client=_wired(fake), root=str(root))
    assert any("invalid redact pattern" in h for h in out.hints)


# ---------------------------------------------------------------- serena migration


async def test_migrate_serena_memories_ships_notes_and_keeps_originals(tmp_path):
    mem_dir = tmp_path / ".serena" / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "project_overview.md").write_text("overview text")
    (mem_dir / "gotchas.md").write_text("gotcha text")
    fake = FakeCognee(datasets=["agent_sessions"])
    out = await mem.migrate_serena_memories(str(tmp_path), client=_wired(fake))
    assert not isinstance(out, MemError)
    assert out.migrated == 2
    assert out.files == ["gotchas.md", "project_overview.md"]
    assert out.node_set == ["project_docs", f"repo:{tmp_path.name}"]
    assert (mem_dir / "project_overview.md").exists()  # originals stay in place
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["node_set"] == ["project_docs", f"repo:{tmp_path.name}"]
    data = str(add_payload["data"])
    assert "gotcha text" in data
    assert "Serena memory: gotchas" in data


async def test_migrate_serena_memories_dry_run_and_empty_dir(tmp_path):
    fake = FakeCognee()
    out = await mem.migrate_serena_memories(str(tmp_path), client=_wired(fake))
    assert not isinstance(out, MemError)
    assert out.migrated == 0
    assert out.files == []
    assert fake.requests == []
    mem_dir = tmp_path / ".serena" / "memories"
    mem_dir.mkdir(parents=True)
    (mem_dir / "note.md").write_text("x" * 4000)
    out = await mem.migrate_serena_memories(str(tmp_path), dry_run=True, client=_wired(fake))
    assert not isinstance(out, MemError)
    assert out.dry_run is True
    assert out.migrated == 0
    assert out.estimate is not None
    assert fake.requests == []


# --------------------------------------------------------------- conventions table


def test_conventions_table_values():
    """The SSOT constants every write path (capture/migrate/onboard) must use verbatim."""
    assert mem.NODE_SET_PROJECT_DOCS == "project_docs"
    assert mem.NODE_SET_SESSION_DIGEST == "session_digest"
    assert mem.NODE_SET_CLAUDE_MEM_IMPORT == "claude_mem_import"
    assert mem.NODE_SET_CODE_MAP == "code_map"
    assert mem.TYPE_TAG_PREFIX == "type:"
    assert mem.CONCEPT_TAG_PREFIX == "concept:"
    assert mem.PROJECT_TAG_PREFIX == "project:"


def test_resolve_dataset_prefers_the_onboarded_marker(tmp_path):
    """A marked repo resolves to its onboarded dataset; everything else falls back to the default."""
    root = tmp_path / "repo"
    root.mkdir()
    assert mem.resolve_dataset(None) == "agent_sessions"
    assert mem.resolve_dataset(str(root)) == "agent_sessions"  # unmarked -> default
    paths.mark_cognee_onboarded(str(root), dataset="kolbe_docs")
    assert mem.resolve_dataset(str(root)) == "kolbe_docs"


# ------------------------------------------------------------------------- memify


async def test_run_memify_stamps_heartbeat_and_fails_open(tmp_path):
    """A successful memify stamps the ``memify`` heartbeat; failures return False, never raise."""
    root = tmp_path / "repo"
    root.mkdir()
    fake = FakeCognee(datasets=["kolbe"])
    assert await mem.run_memify(str(root), "kolbe", client=_wired(fake)) is True
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/memify")
    assert payload == {"datasetName": "kolbe", "runInBackground": True}
    assert "memify" in paths.read_hook_heartbeats(str(root))
    # Fail-open: an unconfigured client reports False and stamps nothing.
    root2 = tmp_path / "repo2"
    root2.mkdir()
    assert await mem.run_memify(str(root2), "kolbe", client=_unconfigured()) is False
    assert "memify" not in paths.read_hook_heartbeats(str(root2))
    # No root (callers without repo context): memify still runs, stamping is skipped.
    fake2 = FakeCognee(datasets=["kolbe"])
    assert await mem.run_memify(None, "kolbe", client=_wired(fake2)) is True


async def test_ingest_runs_memify_after_the_last_cognify():
    """A completed ship ends with one background memify pass over the same dataset."""
    fake = FakeCognee(datasets=["docs"])
    out = await mem.ingest(MemIngestIn(items=["a", "b"], dataset="docs"), client=_wired(fake))
    assert isinstance(out, MemIngestResult)
    tail = [path for m, path, p in fake.requests if path in {"/api/v1/cognify", "/api/v1/memify"}]
    assert tail == ["/api/v1/cognify", "/api/v1/memify"]
    memify_payload = next(p for m, path, p in fake.requests if path == "/api/v1/memify")
    assert memify_payload == {"datasetName": "docs", "runInBackground": True}


async def test_ingest_survives_memify_failure(monkeypatch):
    """The memify pass is best-effort: its failure never fails the ingest that triggered it."""
    fake = FakeCognee(datasets=["docs"])
    client = _wired(fake)

    async def boom(*args, **kwargs):
        msg = "memify exploded"
        raise CogneeError(msg)

    monkeypatch.setattr(client, "memify", boom)
    out = await mem.ingest(MemIngestIn(items=["a"], dataset="docs"), client=client)
    assert isinstance(out, MemIngestResult)
    assert out.ingested == 1
