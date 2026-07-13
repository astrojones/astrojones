"""Capture pipeline: durable enqueue, WAL concurrency, drain shipping, digest fallbacks."""

import json
import threading

import pytest
from repo_agent_harness import agent_hooks, capture, digest_providers
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
    assert add_payload["node_set"] == "agent_actions"
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
    """With a digest, ONE summarized item ships instead of the raw entries."""
    root = str(tmp_path)
    capture.enqueue(root, "stop", {"a": 1})
    capture.enqueue(root, "stop", {"b": 2})

    async def _fake_digest(entries):
        assert len(entries) == 2
        return "DIGEST: two stop events"

    monkeypatch.setattr(digest_providers, "digest", _fake_digest)
    fake = FakeCognee(datasets=[capture.CAPTURE_DATASET])
    brain = capture.BrainCapture(root, _wired(fake))
    assert await brain._drain_once() == 2
    add_payload = next(p for m, path, p in fake.requests if path == "/api/v1/add")
    assert add_payload["data"] == "DIGEST: two stop events"


def test_rendered_entries_carry_event_and_payload(tmp_path):
    capture.enqueue(str(tmp_path), "stop", {"k": "v"})
    fake = FakeCognee()
    brain = capture.BrainCapture(str(tmp_path), _wired(fake))
    rows = brain._fetch_batch()
    line = brain._render(rows[0][1], rows[0][2], rows[0][3])
    assert "stop" in line
    assert json.dumps({"k": "v"}) in line
