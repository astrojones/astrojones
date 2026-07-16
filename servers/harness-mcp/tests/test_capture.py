"""Capture pipeline: durable enqueue, WAL concurrency, drain shipping, digest fallbacks."""

import contextlib
import json
import sqlite3
import threading
from pathlib import Path

import pytest
import yaml
from repo_agent_harness import agent_hooks, capture, digest_providers, paths, secrets
from repo_agent_harness.cognee_client import CogneeClient
from tests.fake_cognee import FakeCognee

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _wired(fake: FakeCognee) -> CogneeClient:
    return CogneeClient(**fake.client_kwargs())


# ------------------------------------------------------------------------ enqueue


def test_enqueue_is_durable_across_connections(tmp_path):
    """A row written by one 'process' is visible to a fresh connection (crash-resume)."""
    capture.enqueue(str(tmp_path), "stop", {"x": 1})
    capture.enqueue(str(tmp_path), "pre_compact", {"y": 2})
    # pending_count opens a brand-new connection — the same thing a restarted server does.
    assert capture.pending_count(str(tmp_path)) == 2


def test_enqueue_never_raises_on_broken_state_dir(tmp_path, monkeypatch):
    """A hook must survive an unwritable queue location silently."""
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", "/dev/null/nope")
    capture.enqueue(str(tmp_path), "stop", {"x": 1})  # must not raise


def test_enqueue_caps_payload_and_queue(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "_MAX_QUEUE_ROWS", 5)
    for i in range(9):
        capture.enqueue(str(tmp_path), "e", {"i": i})
    assert capture.pending_count(str(tmp_path)) == 5


def _queue_payloads(root: str) -> list[str]:
    """Raw payload column straight from the queue DB — what the digest will see."""
    with contextlib.closing(sqlite3.connect(capture.queue_db(root))) as conn:
        return [str(row[0]) for row in conn.execute("SELECT payload FROM capture_queue")]


def test_enqueue_redacts_secret_patterns(tmp_path):
    """The on-disk queue must never hold raw secrets — digest/ship read these rows verbatim."""
    payload = {
        "aws": "AKIAABCDEFGHIJKLMNOP",
        "pem": "-----BEGIN PRIVATE KEY-----",
    }
    capture.enqueue(str(tmp_path), "post_tool_use", payload)
    (stored,) = _queue_payloads(str(tmp_path))
    assert "AKIAABCDEFGHIJKLMNOP" not in stored
    assert "BEGIN PRIVATE KEY" not in stored
    assert secrets.REDACTION in stored


def test_enqueue_redacts_github_token(tmp_path):
    """The gh-token builtin applies through the load() chain (defaults/secrets.yml kept in sync)."""
    capture.enqueue(str(tmp_path), "post_tool_use", {"github": "ghp_" + "a" * 24})
    (stored,) = _queue_payloads(str(tmp_path))
    assert "ghp_" + "a" * 24 not in stored


def test_enqueue_redacts_typed_private_key_header(tmp_path):
    """Typed PEM headers (RSA/EC/OPENSSH) redact through the load() chain (yml kept in sync)."""
    capture.enqueue(str(tmp_path), "post_tool_use", {"pem": "-----BEGIN RSA PRIVATE KEY-----"})
    (stored,) = _queue_payloads(str(tmp_path))
    assert "BEGIN RSA PRIVATE KEY" not in stored


def test_enqueue_redaction_respects_repo_secrets_config(tmp_path, isolated_harness_home):
    """Repo-level policies/secrets.yml patterns apply at enqueue, not just the builtins."""
    pol = isolated_harness_home / "repos" / paths.repo_id(str(tmp_path)) / "policies"
    pol.mkdir(parents=True)
    (pol / "secrets.yml").write_text('redact_patterns:\n  - "CUSTOM-[0-9]{4}"\n')
    capture.enqueue(str(tmp_path), "post_tool_use", {"token": "CUSTOM-1234"})
    (stored,) = _queue_payloads(str(tmp_path))
    assert "CUSTOM-1234" not in stored
    assert secrets.REDACTION in stored


def test_defaults_yml_matches_code_builtin_patterns():
    """Drift tripwire: the packaged yml REPLACES the code defaults in load(), so they must match."""
    yml_path = Path(secrets.__file__).parent / "defaults" / "secrets.yml"
    yml = yaml.safe_load(yml_path.read_text())
    assert yml["redact_patterns"] == secrets.DEFAULT_REDACT_PATTERNS


def test_wal_survives_concurrent_writers(tmp_path):
    """Parallel hook processes (threads with separate connections) all land their rows."""
    root = str(tmp_path)
    threads = [threading.Thread(target=capture.enqueue, args=(root, "post_tool_use", {"n": n})) for n in range(24)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert capture.pending_count(root) == 24


# ------------------------------------------------------------------ hook handlers


def test_stop_hook_enqueues_without_network(repo, monkeypatch):
    """Zero synchronous HTTP: the handler must not even construct a cognee client."""
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COGNEE_BASE_URL", "https://cognee.example")

    def _bomb():
        msg = "hook touched the network client"
        raise AssertionError(msg)

    monkeypatch.setattr("repo_agent_harness.cognee_client.get_client", _bomb)
    assert agent_hooks.stop({"session_id": "s1"}) == {}
    assert capture.pending_count(str(repo)) == 1


def test_pre_compact_hook_enqueues(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COGNEE_BASE_URL", "https://cognee.example")
    assert agent_hooks.pre_compact({"trigger": "auto"}) == {}
    assert capture.pending_count(str(repo)) == 1


def test_hooks_skip_enqueue_when_cognee_unconfigured(repo, monkeypatch):
    """No backend -> no queue: nothing would ever drain it."""
    monkeypatch.chdir(repo)
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    assert agent_hooks.stop({}) == {}
    assert capture.pending_count(str(repo)) == 0


def test_post_tool_use_piggybacks_capture(repo, monkeypatch):
    monkeypatch.chdir(repo)
    monkeypatch.setenv("COGNEE_BASE_URL", "https://cognee.example")
    agent_hooks.post_tool_use({"tool_name": "Edit", "tool_input": {"file_path": str(repo / "src" / "payment.py")}})
    assert capture.pending_count(str(repo)) == 1


# -------------------------------------------------------------------------- drain


async def test_drain_ships_batch_and_deletes_rows(tmp_path, monkeypatch):
    monkeypatch.setenv(digest_providers.PROVIDER_ENV, "off")
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    capture.enqueue(root, "post_tool_use", {"path": "x.py"})
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    brain = capture.BrainCapture(root, _wired(fake))
    shipped = await brain._drain_once()
    assert shipped == 2
    assert capture.pending_count(root) == 0
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["datasetName"] == capture.CAPTURE_DATASET
    assert add_payload["node_set"] == "session_digest"
    assert "stop" in str(add_payload["data"])
    cognify = next(p for m, path, p in fake.requests if path == "/api/v1/cognify")
    assert cognify["runInBackground"] is True


async def test_drain_keeps_rows_when_ship_fails(tmp_path, monkeypatch):
    """Rows are deleted only after a successful ship — an outage loses nothing."""
    monkeypatch.setenv(digest_providers.PROVIDER_ENV, "off")
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    fake.transport_failures = 99
    brain = capture.BrainCapture(root, _wired(fake))
    with pytest.raises(Exception, match=r"unreachable|login"):
        await brain._drain_once()
    assert capture.pending_count(root) == 1


async def test_drain_noop_on_empty_queue(tmp_path, monkeypatch):
    monkeypatch.setenv(digest_providers.PROVIDER_ENV, "off")
    fake = FakeCognee()
    brain = capture.BrainCapture(str(tmp_path), _wired(fake))
    assert await brain._drain_once() == 0
    assert fake.requests == []


async def test_drain_ships_digest_when_available(tmp_path, monkeypatch):
    """With a plaintext digest, ONE summarized item ships instead of the raw entries."""
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    capture.enqueue(root, "stop", {"b": 2})

    async def _fake_digest(entries):
        assert len(entries) == 2
        return digest_providers.DigestResult(text="DIGEST: two stop events")

    monkeypatch.setattr(digest_providers, "digest", _fake_digest)
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    brain = capture.BrainCapture(root, _wired(fake))
    assert await brain._drain_once() == 2
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["data"] == "DIGEST: two stop events"
    assert add_payload["node_set"] == "session_digest"


def _observation(**overrides) -> digest_providers.DigestObservation:
    base = {
        "type": "decision",
        "title": "Pick sqlite",
        "facts": ["WAL survives concurrent writers"],
        "concepts": ["trade-off"],
        "files": ["repo_agent_harness/capture.py"],
    }
    return digest_providers.DigestObservation.model_validate({**base, **overrides})


def _digest_returning(monkeypatch, result: digest_providers.DigestResult) -> None:
    async def _fake_digest(entries):
        return result

    monkeypatch.setattr(digest_providers, "digest", _fake_digest)


async def test_drain_renders_observation_docs_with_type_and_concept_tags(tmp_path, monkeypatch):
    """Observations ship as rendered docs tagged type:<t> / concept:<c> on the base node_set."""
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    _digest_returning(monkeypatch, digest_providers.DigestResult(observations=[_observation()]))
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    brain = capture.BrainCapture(root, _wired(fake))
    assert await brain._drain_once() == 1
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["node_set"] == ["session_digest", "type:decision", "concept:trade-off"]
    doc = add_payload["data"]
    assert doc.startswith("# decision: Pick sqlite\n")
    assert "- WAL survives concurrent writers" in doc
    assert "Concepts: trade-off" in doc
    assert "Files: repo_agent_harness/capture.py" in doc
    # Future-proofing only (temporal search is broken server-side): an ISO-utc Observed line.
    assert "\nObserved: 2" in doc
    assert doc.rstrip().endswith("Z")


async def test_drain_batches_observations_per_node_set_combination(tmp_path, monkeypatch):
    """Same tag combination -> one add call; a different combination -> its own add call."""
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    observations = [
        _observation(title="first"),
        _observation(title="second"),
        _observation(type="bugfix", title="third", concepts=[]),
    ]
    _digest_returning(monkeypatch, digest_providers.DigestResult(observations=observations))
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    brain = capture.BrainCapture(root, _wired(fake))
    assert await brain._drain_once() == 1
    adds = [p for m, path, p in fake.requests if path == "/api/v1/add"]
    assert len(adds) == 2
    assert adds[0]["node_set"] == ["session_digest", "type:decision", "concept:trade-off"]
    assert [d.split("\n", 1)[0] for d in adds[0]["data"]] == ["# decision: first", "# decision: second"]
    assert adds[1]["node_set"] == ["session_digest", "type:bugfix"]
    assert adds[1]["data"].startswith("# bugfix: third")


async def test_drain_ships_to_onboarded_dataset_when_marked(tmp_path, monkeypatch, isolated_harness_home):
    """The onboarding marker's dataset wins over the agent_sessions fallback."""
    monkeypatch.setenv(digest_providers.PROVIDER_ENV, "off")
    root = str(tmp_path)
    paths.mark_cognee_onboarded(root, dataset="proj-ds")
    capture.enqueue(root, "stop", {"a": 1})
    fake = FakeCognee(datasets=["proj-ds"])
    brain = capture.BrainCapture(root, _wired(fake))
    assert await brain._drain_once() == 1
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["datasetName"] == "proj-ds"
    cognify = next(p for m, path, p in fake.requests if path == "/api/v1/cognify")
    assert cognify["datasets"] == ["proj-ds"]


def test_rendered_entries_carry_event_and_payload(tmp_path):
    capture.enqueue(str(tmp_path), "stop", {"k": "v"})
    fake = FakeCognee()
    brain = capture.BrainCapture(str(tmp_path), _wired(fake))
    rows = brain._fetch_batch()
    line = brain._render(rows[0][1], rows[0][2], rows[0][3])
    assert "stop" in line
    assert json.dumps({"k": "v"}) in line


# -------------------------------------------------------------------- integration


def test_capture_constants_match_mem_ssot():
    """Capture keeps local constants (hook hot path must not import mem) — pinned to the SSOT."""
    from repo_agent_harness import mem

    assert capture.CAPTURE_DATASET == mem.DEFAULT_DATASET
    assert capture.CAPTURE_NODE_SET == [mem.NODE_SET_SESSION_DIGEST]


async def test_drain_runs_memify_after_cognify_and_stamps(tmp_path, monkeypatch):
    """Post-ingest memify distills CODING_RULES server-side; a run stamps the memify heartbeat."""
    monkeypatch.setenv(digest_providers.PROVIDER_ENV, "off")
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    brain = capture.BrainCapture(root, _wired(fake))
    await brain._drain_once()
    assert any(path == "/api/v1/memify" for _, path, _ in fake.requests)
    assert "memify" in paths.read_hook_heartbeats(root)


async def test_drain_survives_memify_failure(tmp_path, monkeypatch):
    """Memify is enrichment, not shipping: its failure must not fail the drain or keep rows."""
    monkeypatch.setenv(digest_providers.PROVIDER_ENV, "off")
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    brain = capture.BrainCapture(root, _wired(fake))

    async def _boom(*args, **kwargs):
        msg = "memify down"
        raise RuntimeError(msg)

    monkeypatch.setattr(brain._client, "memify", _boom)
    shipped = await brain._drain_once()
    assert shipped == 1
    assert capture.pending_count(root) == 0
    assert "memify" not in paths.read_hook_heartbeats(root)


def test_ship_groups_canonicalizes_concept_order():
    """Same concept set in different order must land in ONE batch (sorted group key)."""
    obs_a = digest_providers.DigestObservation(
        type="decision", title="a", facts=["f"], concepts=["gotcha", "pattern"], files=[]
    )
    obs_b = digest_providers.DigestObservation(
        type="decision", title="b", facts=["f"], concepts=["pattern", "gotcha"], files=[]
    )
    groups = capture.BrainCapture._ship_groups(digest_providers.DigestResult(observations=[obs_a, obs_b]), [])
    assert len(groups) == 1
