"""CogneeClient transport concerns: auth lifecycle, circuit breaker, idempotent-only retries."""

import pytest
from repo_agent_harness import cognee_client
from repo_agent_harness.cognee_client import (
    CogneeCircuit,
    CogneeClient,
    CogneeError,
    CogneeNotConfiguredError,
    CogneeUnavailableError,
)
from tests.fake_cognee import FakeCognee

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


def test_runtime_disabled_by_default(monkeypatch):
    """Master switch defaults off (conftest strips the env family); armed by 1/true/yes/on."""
    monkeypatch.delenv("REPO_AGENT_HARNESS_COGNEE_ENABLE", raising=False)
    assert cognee_client.cognee_runtime_enabled() is False
    for val in ("1", "true", "YES", "on"):
        monkeypatch.setenv("REPO_AGENT_HARNESS_COGNEE_ENABLE", val)
        assert cognee_client.cognee_runtime_enabled() is True
    for val in ("0", "", "off", "no"):
        monkeypatch.setenv("REPO_AGENT_HARNESS_COGNEE_ENABLE", val)
        assert cognee_client.cognee_runtime_enabled() is False


async def test_login_is_lazy_and_shared():
    """No login happens at construction; the first request logs in once, later ones reuse it."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    assert fake.logins == 0
    assert await client.datasets() == [{"name": "kolbe", "id": "id-kolbe"}]
    assert await client.datasets() == [{"name": "kolbe", "id": "id-kolbe"}]
    assert fake.logins == 1
    await client.aclose()


async def test_expired_token_refreshes_once_and_succeeds():
    """A 401 mid-session invalidates the cached token and the resend carries a fresh login."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    await client.datasets()
    fake.expire_token_once = True
    assert await client.datasets() == [{"name": "kolbe", "id": "id-kolbe"}]
    assert fake.logins == 2
    await client.aclose()


async def test_not_configured_fails_closed_with_hint():
    """No URL + no credentials -> CogneeNotConfiguredError naming the env vars."""
    client = CogneeClient(url=None, auth=None, key=None)
    assert client.configured is False
    with pytest.raises(CogneeNotConfiguredError, match="COGNEE_BASE_URL"):
        await client.datasets()


async def test_idempotent_reads_retry_on_transport_error():
    """A GET survives two transport failures (3 attempts); the fake sees the eventual request."""
    fake = FakeCognee(datasets=["kolbe"])
    fake.transport_failures = 2
    client = CogneeClient(**fake.client_kwargs())
    assert await client.datasets() == [{"name": "kolbe", "id": "id-kolbe"}]
    await client.aclose()


async def test_writes_are_never_blind_retried():
    """A POST /add dies on the first transport failure — a duplicated add is worse than a failed one."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    await client.datasets()  # login + warm
    fake.transport_failures = 1
    with pytest.raises(CogneeUnavailableError, match="after 1 attempt"):
        await client.add(["x"], "kolbe", None)
    assert all(path != "/api/v1/add" for _, path, _ in fake.requests)
    await client.aclose()


async def test_http_error_raises_with_status():
    """Non-2xx responses surface as CogneeError carrying the status code."""
    fake = FakeCognee()
    client = CogneeClient(**fake.client_kwargs())
    with pytest.raises(CogneeError) as exc_info:
        await client.request("GET", "/api/v1/nonexistent")
    assert exc_info.value.status == 404


async def test_circuit_opens_after_threshold_and_recovers_via_probe():
    """5 consecutive failures open the circuit; after the window one probe may close it again."""
    now = [0.0]
    circuit = CogneeCircuit(clock=lambda: now[0])
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs(), circuit=circuit)
    await client.datasets()  # login once so failures below are pure transport
    fake.transport_failures = 5
    for _ in range(5):
        with pytest.raises(CogneeUnavailableError):
            await client.request("GET", "/api/v1/datasets")  # non-idempotent: 1 attempt = 1 failure
    assert circuit.state == "open"
    # While open, requests are refused WITHOUT touching the transport.
    seen = len(fake.requests)
    with pytest.raises(CogneeUnavailableError, match="circuit open"):
        await client.datasets()
    assert len(fake.requests) == seen
    # After the open window the half-open probe goes through and closes the circuit.
    now[0] = 121.0
    assert circuit.state == "half_open"
    assert await client.datasets() == [{"name": "kolbe", "id": "id-kolbe"}]
    assert circuit.state == "closed"
    await client.aclose()


async def test_failed_probe_reopens_immediately():
    """A failure during half-open re-trips the circuit without needing 5 more failures."""
    now = [0.0]
    circuit = CogneeCircuit(clock=lambda: now[0])
    for _ in range(5):
        circuit.record_failure()
    assert circuit.state == "open"
    now[0] = 121.0
    assert circuit.state == "half_open"
    circuit.record_failure()
    now[0] = 122.0
    assert circuit.state == "open"


def test_singleton_reuses_and_resets():
    """get_client returns one shared instance until reset_client drops it."""
    cognee_client.reset_client()
    a = cognee_client.get_client()
    assert cognee_client.get_client() is a
    cognee_client.reset_client()
    assert cognee_client.get_client() is not a
    cognee_client.reset_client()


def test_env_resolution(monkeypatch):
    """Env config: base URL is normalized, both credential spellings work."""
    monkeypatch.setenv("COGNEE_BASE_URL", "https://x.example/")
    assert cognee_client.base_url() == "https://x.example"
    monkeypatch.setenv("COGNEE_USER_EMAIL", "a@b.c")
    monkeypatch.setenv("COGNEE_USER_PASSWORD", "s3cret")
    assert cognee_client.credentials() == ("a@b.c", "s3cret")
    monkeypatch.delenv("COGNEE_USER_EMAIL")
    monkeypatch.delenv("COGNEE_USER_PASSWORD")
    monkeypatch.setenv("COGNEE_USERNAME", "d@e.f")
    monkeypatch.setenv("COGNEE_PASSWORD", "pw2")
    assert cognee_client.credentials() == ("d@e.f", "pw2")


# -------------------------------------------------- local-cognee fallback resolution


def _write_endpoint(tmp_path, monkeypatch, **fields):
    import json

    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path))
    ep = tmp_path / "cognee" / "endpoint.json"
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text(json.dumps(fields), encoding="utf-8")


def test_base_url_prefers_remote_over_local(tmp_path, monkeypatch):
    """A configured remote always wins; the local endpoint file is never consulted for it."""
    _write_endpoint(tmp_path, monkeypatch, base_url="http://127.0.0.1:8765", email="h@l.io", password="pw")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://remote.example/")
    assert cognee_client.base_url() == "https://remote.example"


def test_base_url_falls_back_to_local(tmp_path, monkeypatch):
    """No remote -> the local endpoint file's base_url is used."""
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    _write_endpoint(tmp_path, monkeypatch, base_url="http://127.0.0.1:8765", email="h@l.io", password="pw")
    assert cognee_client.base_url() == "http://127.0.0.1:8765"


def test_credentials_use_local_when_no_remote(tmp_path, monkeypatch):
    """No remote and no env creds -> the local endpoint file's email/password."""
    for v in ("COGNEE_BASE_URL", "COGNEE_USER_EMAIL", "COGNEE_USER_PASSWORD", "COGNEE_USERNAME", "COGNEE_PASSWORD"):
        monkeypatch.delenv(v, raising=False)
    _write_endpoint(tmp_path, monkeypatch, base_url="http://127.0.0.1:8765", email="h@l.io", password="pw")
    assert cognee_client.credentials() == ("h@l.io", "pw")


def test_credentials_env_wins_over_local(tmp_path, monkeypatch):
    """Env creds (remote user) always win over the local endpoint file."""
    _write_endpoint(tmp_path, monkeypatch, base_url="http://127.0.0.1:8765", email="local@l.io", password="localpw")
    monkeypatch.setenv("COGNEE_BASE_URL", "https://remote.example")
    monkeypatch.setenv("COGNEE_USER_EMAIL", "env@e.io")
    monkeypatch.setenv("COGNEE_USER_PASSWORD", "envpw")
    assert cognee_client.credentials() == ("env@e.io", "envpw")


def test_garbage_endpoint_is_ignored(tmp_path, monkeypatch):
    """A missing/garbage endpoint file fails closed to no local endpoint (hot-path safe)."""
    monkeypatch.delenv("COGNEE_BASE_URL", raising=False)
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path))
    ep = tmp_path / "cognee" / "endpoint.json"
    ep.parent.mkdir(parents=True, exist_ok=True)
    ep.write_text("{not json", encoding="utf-8")
    assert cognee_client.base_url() is None


def test_configured_true_for_local_only(tmp_path, monkeypatch):
    """With only a local endpoint file, the client is configured (flips capture + recall on)."""
    env = (
        "COGNEE_BASE_URL",
        "COGNEE_API_KEY",
        "COGNEE_USER_EMAIL",
        "COGNEE_USER_PASSWORD",
        "COGNEE_USERNAME",
        "COGNEE_PASSWORD",
    )
    for v in env:
        monkeypatch.delenv(v, raising=False)
    _write_endpoint(tmp_path, monkeypatch, base_url="http://127.0.0.1:8765", email="h@l.io", password="pw")
    assert CogneeClient().configured is True


async def test_search_requires_dataset():
    """Span-all search is disallowed.

    A datasets-less query reads every dataset the server hosts — on a shared deployment
    that includes demo/junk corpora (observed live: recall returned a stored "Got it."
    from an unrelated demo dataset).
    """
    fake = FakeCognee()
    client = CogneeClient(**fake.client_kwargs())
    with pytest.raises(ValueError, match="dataset"):
        await client.search("q", "CHUNKS", "", 5)


async def test_delete_dataset_issues_delete():
    """delete_dataset(id) is a single-attempt DELETE of the whole dataset (eval teardown)."""
    fake = FakeCognee(datasets=["ds"])
    client = CogneeClient(**fake.client_kwargs())
    await client.delete_dataset("id-ds")
    method, _, _ = next((m, p, pl) for m, p, pl in fake.requests if p == "/api/v1/datasets/id-ds")
    assert method == "DELETE"
    await client.aclose()


# ------------------------------------------------------------------ tier-1 wrappers


async def test_memify_posts_camelcase_payload():
    """The memify payload is camelCase; nodeName (per MemifyPayloadDTO) appears only when given."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    await client.memify("kolbe", run_in_background=False)
    method, _, payload = next((m, p, pl) for m, p, pl in fake.requests if p == "/api/v1/memify")
    assert method == "POST"
    assert payload == {"datasetName": "kolbe", "runInBackground": False}
    await client.memify("kolbe", node_name=["memories"])
    payload = [pl for _, p, pl in fake.requests if p == "/api/v1/memify"][-1]
    assert payload == {"datasetName": "kolbe", "runInBackground": True, "nodeName": ["memories"]}
    await client.aclose()


async def test_search_forwards_node_name_filter():
    """search() sends nodeName (the belongs_to_set filter) only when given; omitted otherwise."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    await client.search("q", "CHUNKS", "kolbe", 5)
    payload = [pl for _, p, pl in fake.requests if p == "/api/v1/search"][-1]
    assert "nodeName" not in payload
    await client.search("q", "CHUNKS", "kolbe", 5, node_name=["session_digest"])
    payload = [pl for _, p, pl in fake.requests if p == "/api/v1/search"][-1]
    assert payload["nodeName"] == ["session_digest"]
    await client.aclose()


async def test_update_data_patches_multipart_like_add():
    """PATCH /api/v1/update speaks the same multipart dialect as /add."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    await client.update_data(["revised text"], node_set=["project_docs"])
    method, _, payload = next((m, p, pl) for m, p, pl in fake.requests if p == "/api/v1/update")
    assert method == "PATCH"
    assert payload["data"] == "revised text"
    assert payload["node_set"] == "project_docs"
    await client.aclose()


async def test_dataset_data_is_an_idempotent_read():
    """Listing a dataset's data items is an idempotent read (it survives a transport blip)."""
    fake = FakeCognee(datasets=["kolbe"])
    fake.data_items["id-kolbe"] = [{"id": "d-1", "name": "item"}]
    client = CogneeClient(**fake.client_kwargs())
    await client.datasets()  # login + warm so the failure below is pure transport
    fake.transport_failures = 1
    assert await client.dataset_data("id-kolbe") == [{"id": "d-1", "name": "item"}]
    method, path, _ = next((m, p, pl) for m, p, pl in fake.requests if p.endswith("/data"))
    assert (method, path) == ("GET", "/api/v1/datasets/id-kolbe/data")
    await client.aclose()


async def test_delete_data_is_a_single_attempt_write():
    """DELETE of one data item hits the nested route and is never blind-retried."""
    fake = FakeCognee(datasets=["kolbe"])
    fake.data_items["id-kolbe"] = [{"id": "d-1"}, {"id": "d-2"}]
    client = CogneeClient(**fake.client_kwargs())
    await client.delete_data("id-kolbe", "d-1")
    method, path, _ = next((m, p, pl) for m, p, pl in fake.requests if "/data/" in p)
    assert (method, path) == ("DELETE", "/api/v1/datasets/id-kolbe/data/d-1")
    assert fake.data_items["id-kolbe"] == [{"id": "d-2"}]
    fake.transport_failures = 1
    with pytest.raises(CogneeUnavailableError, match="after 1 attempt"):
        await client.delete_data("id-kolbe", "d-2")
    await client.aclose()


async def test_improve_posts_camelcase_flags():
    """The improve payload carries camelCase flags; datasetName appears only when given."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    await client.improve()
    method, _, payload = next((m, p, pl) for m, p, pl in fake.requests if p == "/api/v1/improve")
    assert method == "POST"
    assert payload == {"runInBackground": True, "buildGlobalContextIndex": False}
    await client.improve("kolbe", build_global_context_index=True, run_in_background=False)
    payload = [pl for _, p, pl in fake.requests if p == "/api/v1/improve"][-1]
    assert payload == {"datasetName": "kolbe", "runInBackground": False, "buildGlobalContextIndex": True}
    await client.aclose()


async def test_export_markdown_is_an_idempotent_read():
    """The activity export returns the markdown (a JSON string) and retries as a read."""
    fake = FakeCognee(datasets=["kolbe"])
    fake.exports["id-kolbe"] = "# kolbe activity\n- added"
    client = CogneeClient(**fake.client_kwargs())
    await client.datasets()  # login + warm so the failure below is pure transport
    fake.transport_failures = 1
    assert await client.export_markdown("id-kolbe") == "# kolbe activity\n- added"
    method, path, _ = next((m, p, pl) for m, p, pl in fake.requests if p.startswith("/api/v1/activity"))
    assert (method, path) == ("GET", "/api/v1/activity/export/id-kolbe")
    await client.aclose()


async def test_export_markdown_handles_raw_markdown_body():
    """A server that returns the markdown verbatim (not JSON-encoded) is handled too."""
    fake = FakeCognee(datasets=["kolbe"])
    fake.exports_raw = True
    fake.exports["id-kolbe"] = "# kolbe activity\n- added"
    client = CogneeClient(**fake.client_kwargs())
    assert await client.export_markdown("id-kolbe") == "# kolbe activity\n- added"
    await client.aclose()


# --------------------------------------------- coexistence memory-API wrappers (P2)


async def test_remember_posts_multipart_and_omits_node_set_when_none():
    """remember() POSTs /api/v1/remember multipart; node_set omitted when None, present when given."""
    fake = FakeCognee(datasets=["cm_astrojones"])
    client = CogneeClient(**fake.client_kwargs())
    await client.remember(["doc one"], "cm_astrojones", None)
    method, path, payload = next((m, p, pl) for m, p, pl in fake.requests if p == "/api/v1/remember")
    assert (method, path) == ("POST", "/api/v1/remember")
    assert payload["data"] == "doc one"
    assert payload["datasetName"] == "cm_astrojones"
    assert "node_set" not in payload
    await client.remember(["doc two"], "cm_astrojones", ["claude_mem_mirror"])
    payload = [pl for _, p, pl in fake.requests if p == "/api/v1/remember"][-1]
    assert payload["node_set"] == "claude_mem_mirror"
    await client.aclose()


async def test_recall_posts_pinned_only_context_body():
    """recall() sends the camelCase onlyContext/topK body with datasets pinned and scope listed."""
    fake = FakeCognee(datasets=["cm_astrojones"])
    client = CogneeClient(**fake.client_kwargs())
    await client.recall("what did we decide", ["cm_astrojones"])
    method, path, payload = next((m, p, pl) for m, p, pl in fake.requests if p == "/api/v1/recall")
    assert (method, path) == ("POST", "/api/v1/recall")
    assert payload == {
        "query": "what did we decide",
        "datasets": ["cm_astrojones"],
        "scope": ["graph"],
        "onlyContext": True,
        "topK": 5,
    }
    await client.aclose()


async def test_recall_requires_datasets():
    """An unpinned recall (empty datasets) raises before any network call — no login, no request."""
    fake = FakeCognee()
    client = CogneeClient(**fake.client_kwargs())
    with pytest.raises(ValueError, match="datasets"):
        await client.recall("q", [])
    assert not fake.requests
    await client.aclose()


async def test_forget_posts_dataset_body():
    """forget() is a single-attempt POST /api/v1/forget carrying the {dataset} body."""
    fake = FakeCognee(datasets=["cm_astrojones"])
    client = CogneeClient(**fake.client_kwargs())
    await client.forget("cm_astrojones")
    method, path, payload = next((m, p, pl) for m, p, pl in fake.requests if p == "/api/v1/forget")
    assert (method, path) == ("POST", "/api/v1/forget")
    assert payload == {"dataset": "cm_astrojones"}
    await client.aclose()


async def test_cognify_status_scopes_pipeline_param_only_when_given():
    """cognify_status polls datasets/status; the pipeline param appears only when passed."""
    fake = FakeCognee(datasets=["kolbe"])
    client = CogneeClient(**fake.client_kwargs())
    await client.cognify_status("id-kolbe")
    params = [pl for _, p, pl in fake.requests if p == "/api/v1/datasets/status"][-1]
    assert params["dataset"] == "id-kolbe"
    assert "pipeline" not in params
    await client.cognify_status("id-kolbe", pipeline="cognify_pipeline")
    params = [pl for _, p, pl in fake.requests if p == "/api/v1/datasets/status"][-1]
    assert params["dataset"] == "id-kolbe"
    assert params["pipeline"] == "cognify_pipeline"
    await client.aclose()
