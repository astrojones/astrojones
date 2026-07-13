"""mem_* business logic: contracts, cost pre-flight, serial-first cognify, doctor sentinels."""

import time

import pytest
from repo_agent_harness import mem
from repo_agent_harness.cognee_client import CogneeClient
from tests.fake_cognee import FakeCognee

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _wired(fake: FakeCognee) -> CogneeClient:
    return CogneeClient(**fake.client_kwargs())


# ------------------------------------------------------------------------- search


async def test_search_returns_shaped_result():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.search("what happened?", dataset="kolbe", client=_wired(fake))
    assert out == {
        "results": [{"text": "canned:GRAPH_COMPLETION"}],
        "search_type": "GRAPH_COMPLETION",
        "dataset": "kolbe",
    }
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["datasets"] == ["kolbe"]
    assert payload["topK"] == 10


async def test_search_rejects_unknown_type_without_network():
    fake = FakeCognee()
    out = await mem.search("q", search_type="FEELING_LUCKY", client=_wired(fake))
    assert "unsupported search_type" in out["error"]
    assert fake.requests == []


async def test_rules_is_a_coding_rules_search():
    fake = FakeCognee()
    out = await mem.rules("python error handling", client=_wired(fake))
    assert out["search_type"] == "CODING_RULES"
    payload = next(p for m, path, p in fake.requests if path == "/api/v1/search")
    assert payload["searchType"] == "CODING_RULES"


async def test_search_maps_client_failure_to_error_dict():
    out = await mem.search("q", client=CogneeClient(url=None, auth=None, key=None))
    assert "cognee not configured" in out["error"]
    assert "COGNEE_BASE_URL" in out["hint"]


# ----------------------------------------------------------------------- remember


async def test_remember_adds_then_background_cognifies():
    fake = FakeCognee(datasets=["agent_sessions"])
    out = await mem.remember("fact", node_set=["project_docs"], client=_wired(fake))
    assert out["queued"] is True
    assert out["dataset"] == "agent_sessions"
    assert out["add_id"]
    calls = [(path, p) for m, path, p in fake.requests if path in {"/api/v1/add", "/api/v1/cognify"}]
    assert [path for path, _ in calls] == ["/api/v1/add", "/api/v1/cognify"]
    add_payload = calls[0][1]
    assert add_payload["datasetName"] == "agent_sessions"
    assert add_payload["node_set"] == "project_docs"
    cognify_payload = calls[1][1]
    assert cognify_payload["runInBackground"] is True


async def test_remember_folds_metadata_into_text():
    fake = FakeCognee(datasets=["agent_sessions"])
    await mem.remember("fact", metadata={"repo": "astrojones"}, client=_wired(fake))
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert "repo=astrojones" in add_payload["data"]


# ------------------------------------------------------------------------- ingest


async def test_ingest_dry_run_estimates_without_writing():
    fake = FakeCognee()
    out = await mem.ingest(["x" * 4000], "docs", dry_run=True, client=_wired(fake))
    assert out["dry_run"] is True
    assert out["estimated_tokens"] == 1000
    assert out["estimated_cost_usd"] > 0
    assert fake.requests == []


async def test_ingest_refuses_expensive_run_without_confirm(monkeypatch):
    monkeypatch.setenv("COGNEE_INGEST_COST_LIMIT_USD", "0.001")
    fake = FakeCognee()
    out = await mem.ingest(["x" * 400_000], "docs", client=_wired(fake))
    assert "ingest refused" in out["error"]
    assert "confirm=true" in out["hint"]
    assert fake.requests == []


async def test_ingest_fresh_dataset_is_serial_first():
    """On a fresh dataset: awaited single-doc cognify (batch=1) BEFORE the bulk background one."""
    fake = FakeCognee(datasets=["other"])
    out = await mem.ingest(["doc-a", "doc-b", "doc-c"], "fresh_ds", confirm=True, client=_wired(fake))
    assert out["fresh_dataset"] is True
    assert out["serial_first"] is True
    assert out["ingested"] == 3
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
    out = await mem.ingest(["a", "b"], "docs", client=_wired(fake))
    assert out["fresh_dataset"] is False
    cognifies = [p for m, path, p in fake.requests if path == "/api/v1/cognify"]
    assert len(cognifies) == 1
    assert cognifies[0]["runInBackground"] is True


async def test_ingest_empty_items_is_an_error():
    out = await mem.ingest([], "docs", client=CogneeClient(url=None, auth=None, key=None))
    assert out["error"] == "no items to ingest"


# -------------------------------------------------------------------------- stats


async def test_stats_reports_existence_and_honest_unsupported_counts():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.stats("kolbe", client=_wired(fake))
    assert out["dataset_id"] == "id-kolbe"
    assert out["node_counts_by_type"]["error"] == "not supported"


async def test_stats_unknown_dataset_lists_available():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.stats("nope", client=_wired(fake))
    assert "not found" in out["error"]
    assert out["available"] == ["kolbe"]


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
    individuals = {"kolbe-api": "Service", "Zeugnis": "Artifact"}
    first = await mem.ontology(individuals, client=_wired(fake))
    assert first["uploaded"] is True
    assert first["types"] == ["Artifact", "Service"]
    second = await mem.ontology(individuals, client=_wired(fake))
    assert second["uploaded"] is False
    assert second["ontology_key"] == first["ontology_key"]
    uploads = [p for m, path, p in fake.requests if path == "/api/v1/ontologies"]
    assert len(uploads) == 1


async def test_ontology_empty_input_is_an_error():
    out = await mem.ontology({}, client=CogneeClient(url=None, auth=None, key=None))
    assert "no individuals" in out["error"]


# ------------------------------------------------------------------------- doctor


async def test_doctor_green_against_live_fake():
    fake = FakeCognee(datasets=["kolbe"])
    out = await mem.doctor(client=_wired(fake))
    assert out["configured"] is True
    assert out["reachable"] is True
    assert out["authenticated"] is True
    assert out["datasets"] == ["kolbe"]


async def test_doctor_unconfigured_hints_env_vars():
    out = await mem.doctor(client=CogneeClient(url=None, auth=None, key=None))
    assert out["configured"] is False
    assert any("COGNEE_BASE_URL" in h for h in out["hints"])


async def test_doctor_unreachable_server_is_reported_not_raised():
    fake = FakeCognee()
    fake.transport_failures = 99
    out = await mem.doctor(client=_wired(fake))
    assert out["reachable"] is False
    assert any("health probe failed" in h for h in out["hints"])


async def test_doctor_detects_live_claude_mem_capture(tmp_path, monkeypatch):
    fake = FakeCognee(datasets=["kolbe"])
    db = tmp_path / ".claude-mem" / "claude-mem.db"
    db.parent.mkdir()
    db.write_text("x")
    monkeypatch.setattr(mem, "_CLAUDE_MEM_DB", db)
    out = await mem.doctor(client=_wired(fake))
    assert any("claude-mem capture looks LIVE" in h for h in out["hints"])
    # An old, quiet DB is not flagged.
    old = time.time() - 3600
    import os

    os.utime(db, (old, old))
    out = await mem.doctor(client=_wired(fake))
    assert not any("claude-mem" in h for h in out["hints"])


async def test_doctor_detects_pending_cognee_plugin_captures(tmp_path, monkeypatch):
    fake = FakeCognee(datasets=["kolbe"])
    pending = tmp_path / ".cognee-plugin" / "claude-code" / "pending"
    pending.mkdir(parents=True)
    (pending / "cap.json").write_text("{}")
    monkeypatch.setattr(mem, "_COGNEE_PLUGIN_DIR", tmp_path / ".cognee-plugin")
    out = await mem.doctor(client=_wired(fake))
    assert any("pending captures" in h for h in out["hints"])
