"""CogneeSync: the claude-mem -> cognee mirror loop.

Points the sync at a temp claude-mem fixture DB (DDL/insert helpers reused from
``test_claude_mem_reader``) and a ``FakeCognee`` client. Asserts the ship->verify ordering,
the stop-on-failure watermark contract, the cycle-level breaker, and dataset sanitisation.
"""

import pytest
from repo_agent_harness.cognee_client import CogneeCircuit, CogneeClient
from repo_agent_harness.cognee_sync import CogneeSync
from repo_agent_harness.sync_ledger import SyncLedger
from tests.fake_cognee import FakeCognee
from tests.test_claude_mem_reader import _NOT_NULL_OBS, _insert_obs, _store

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _wired(fake: FakeCognee, *, never_trip: bool = False) -> CogneeClient:
    kwargs = fake.client_kwargs()
    if never_trip:
        # Disable the transport-level breaker so it can't pre-empt the sync-cycle breaker under test.
        kwargs["circuit"] = CogneeCircuit(threshold=10_000)
    return CogneeClient(**kwargs)


def _remember_count(fake: FakeCognee) -> int:
    return sum(1 for _m, path, _p in fake.requests if path == "/api/v1/remember")


# ------------------------------------------------------------------ dataset naming (pure)


def test_dataset_defaults_to_repo_basename():
    """Project == repo basename by default -> cm_<basename>."""
    sync = CogneeSync("/home/dev/astrojones")
    assert sync._project == "astrojones"
    assert sync._dataset == "cm_astrojones"


# ------------------------------------------------------------------ ordering (I3)


async def test_cycle_ships_then_verifies_then_records_ok(tmp_path):
    db = _store(tmp_path)
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "myrepo", "title": "a real discovery"})
    fake = FakeCognee()
    ledger = SyncLedger()
    sync = CogneeSync(str(tmp_path), db=db, project="myrepo", ledger=ledger)
    sync._bind(_wired(fake))

    await sync._cycle()

    # remember precedes the datasets/status verify poll
    seq = [path for _m, path, _p in fake.requests if path in {"/api/v1/remember", "/api/v1/datasets/status"}]
    assert seq[:2] == ["/api/v1/remember", "/api/v1/datasets/status"]
    # a verified-ok ledger row (advancing the watermark) lands only after BOTH succeeded
    assert ledger.watermark("obs") == 1


async def test_cycle_replay_dedup_skips_already_ok(tmp_path):
    """A second cycle over the same store re-ships nothing (content_hash dedup)."""
    db = _store(tmp_path)
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "myrepo", "title": "one discovery"})
    fake = FakeCognee()
    ledger = SyncLedger()
    sync = CogneeSync(str(tmp_path), db=db, project="myrepo", ledger=ledger)
    sync._bind(_wired(fake))

    await sync._cycle()
    first = _remember_count(fake)
    await sync._cycle()
    assert _remember_count(fake) == first  # watermark advanced + dedup: no re-ship


# ------------------------------------------------------------------ failure-injection (watermark)


async def test_failed_cycle_leaves_watermark_unmoved(tmp_path):
    db = _store(tmp_path)
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "myrepo", "title": "will fail"})
    fake = FakeCognee()
    client = _wired(fake, never_trip=True)
    await client.datasets()  # warm the auth token so the failure hits remember, not login
    fake.server_error_times = 100  # every subsequent request 500s
    ledger = SyncLedger()
    sync = CogneeSync(str(tmp_path), db=db, project="myrepo", ledger=ledger)
    sync._bind(client)

    await sync._cycle()

    assert any(path == "/api/v1/remember" for _m, path, _p in fake.requests)  # it did try
    assert ledger.watermark("obs") == 0  # ...but nothing verified ok -> watermark unmoved


# ------------------------------------------------------------------ breaker


async def test_breaker_opens_after_five_failed_cycles_then_retries(tmp_path):
    db = _store(tmp_path)
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "myrepo", "title": "always down"})
    fake = FakeCognee()
    client = _wired(fake, never_trip=True)
    await client.datasets()  # warm auth
    fake.server_error_times = 10_000  # fail forever
    clock = {"t": 0.0}
    sync = CogneeSync(str(tmp_path), db=db, project="myrepo", ledger=SyncLedger(), clock=lambda: clock["t"])
    sync._bind(client)

    for _ in range(5):
        await sync._cycle()
    assert _remember_count(fake) == 5  # one remember attempt per failed cycle

    await sync._cycle()  # breaker now open -> the whole remember/poll block is skipped
    assert _remember_count(fake) == 5  # no new remember

    clock["t"] += 601.0  # past the 10-minute cool-off -> half-open retry
    await sync._cycle()
    assert _remember_count(fake) == 6  # retried once the window elapsed


# ------------------------------------------------------------------ dataset naming (I5)


async def test_remember_uses_sanitized_dataset_never_raw(tmp_path):
    db = _store(tmp_path)
    _insert_obs(db, **{**_NOT_NULL_OBS, "project": "My.Repo-Name", "title": "sanitise me"})
    fake = FakeCognee()
    ledger = SyncLedger()
    sync = CogneeSync(str(tmp_path), db=db, project="My.Repo-Name", ledger=ledger)
    sync._bind(_wired(fake))

    await sync._cycle()

    remember = next(payload for _m, path, payload in fake.requests if path == "/api/v1/remember")
    assert remember["datasetName"] == "cm_my_repo_name"  # lowercased, non [a-z0-9_] -> _
    assert remember["datasetName"] != "My.Repo-Name"  # never the raw project value


# ------------------------------------------------------------------ restart safety


async def test_start_clears_stop_event_for_clean_restart(tmp_path):
    """A restart after stop() must reset the stop signal, else run() would exit at once (dead loop)."""
    db = _store(tmp_path)
    fake = FakeCognee()
    client = _wired(fake)
    sync = CogneeSync(str(tmp_path), db=db, project="myrepo", ledger=SyncLedger())

    sync.start(client)
    await sync.stop()
    assert sync._stop.is_set()  # stop() signalled the loop to exit

    sync.start(client)
    assert not sync._stop.is_set()  # restart cleared it -> run() will actually loop
    await sync.stop()
