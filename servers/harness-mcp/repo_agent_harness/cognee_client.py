"""Async client for a remote cognee server: bearer auth, circuit breaker, bounded retries.

The harness treats cognee as a *remote* durable-memory store (zero local footprint): this
module owns the transport concerns — login, token refresh, failure isolation — so the
``mem_*`` business logic in ``mem.py`` stays a thin mapping from tool contracts to endpoints.

Contracts verified against the live deployment's OpenAPI (2026-07-13): ``/api/v1/search``
takes camelCase ``searchType``/``topK``/``datasets`` (names, not ids); ``/api/v1/add`` and
``/api/v1/remember`` are multipart with a repeated ``node_set`` field; ``/api/v1/cognify``
exposes ``runInBackground``/``dataPerBatch``/``chunksPerBatch``; ``/health`` exists.
"""

from __future__ import annotations

import asyncio
import json as _json  # aliased: ``request``/``_send`` take a ``json`` keyword parameter
import os
import time
from http import HTTPStatus
from typing import TYPE_CHECKING

import httpx

from repo_agent_harness import paths

if TYPE_CHECKING:
    from collections.abc import Callable

# Decoded JSON body — cognee endpoints return objects or arrays; some return empty bodies.
type Json = dict | list | str | int | float | bool | None

DEFAULT_TIMEOUT_S = 30.0
# Retries apply to idempotent reads ONLY — a blind /add or /cognify retry could double-write.
IDEMPOTENT_ATTEMPTS = 3
RETRY_BACKOFF_S = 0.5

NOT_CONFIGURED_HINT = (
    "set COGNEE_BASE_URL plus COGNEE_USER_EMAIL/COGNEE_USER_PASSWORD (or COGNEE_API_KEY) in the environment, "
    "or run `repo-agent-harness cognee-local up` to bring up a local instance"
)


class CogneeError(RuntimeError):
    """Base error for cognee client failures; carries the HTTP status when there is one."""

    def __init__(self, message: str, status: int | None = None) -> None:
        """Store the message and the originating HTTP status (None for transport errors)."""
        super().__init__(message)
        self.status = status


class CogneeNotConfiguredError(CogneeError):
    """Raised when COGNEE_BASE_URL (or any credential) is missing — fail closed, with a hint."""

    def __init__(self) -> None:
        """Build the fixed not-configured message with the actionable env-var hint."""
        super().__init__(f"cognee not configured: {NOT_CONFIGURED_HINT}")


class CogneeUnavailableError(CogneeError):
    """Raised when the server is unreachable or the circuit breaker is open."""


class CogneeAuthError(CogneeError):
    """Raised when login fails or a refreshed token is still rejected."""


def _remote_base_url() -> str | None:
    """The *remote* cognee root URL from COGNEE_BASE_URL, or None when unset."""
    raw = (os.environ.get("COGNEE_BASE_URL") or "").strip()
    return raw.rstrip("/") or None


def _local_endpoint() -> dict | None:
    """The local-cognee endpoint descriptor (base_url + creds), or None.

    Read on the hook hot path via ``configured``, so it must be cheap and fail closed: a
    missing or garbage file — or one without a base_url — yields None, never an exception.
    """
    try:
        with paths.cognee_endpoint_file().open(encoding="utf-8") as f:
            data = _json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and data.get("base_url") else None


def base_url() -> str | None:
    """The cognee root URL: a configured remote (COGNEE_BASE_URL) wins, else a local instance."""
    remote = _remote_base_url()
    if remote:
        return remote
    url = (_local_endpoint() or {}).get("base_url")
    return str(url).rstrip("/") if url else None


def credentials() -> tuple[str, str] | None:
    """(email, password) for the form login; env spellings win, else the local endpoint's.

    The local endpoint is consulted only when no remote is configured, so a remote user's
    behavior is byte-for-byte unchanged.
    """
    email = (os.environ.get("COGNEE_USER_EMAIL") or os.environ.get("COGNEE_USERNAME") or "").strip()
    password = os.environ.get("COGNEE_USER_PASSWORD") or os.environ.get("COGNEE_PASSWORD") or ""
    if email and password:
        return (email, password)
    if not _remote_base_url():
        ep = _local_endpoint()
        if ep and ep.get("email") and ep.get("password"):
            return (str(ep["email"]), str(ep["password"]))
    return None


def api_key() -> str | None:
    """Static API key (X-Api-Key header) — the login-less auth path, when provisioned."""
    return (os.environ.get("COGNEE_API_KEY") or "").strip() or None


class CogneeAuth:
    """Form login against ``/api/v1/auth/login``; bearer token held in memory only.

    The token is never persisted anywhere — a fresh process logs in again. ``invalidate()``
    plus the client's single 401 retry implements refresh-on-expiry without clock math.
    """

    def __init__(self, email: str, password: str) -> None:
        """Hold the form-login credentials; no network I/O happens here."""
        self._email = email
        self._password = password
        self._token: str | None = None
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        """Drop the cached token so the next request performs a fresh login."""
        self._token = None

    async def header(self, http: httpx.AsyncClient) -> dict[str, str]:
        """Return the Authorization header, logging in first if no token is cached."""
        async with self._lock:  # single-flight: concurrent tool calls share one login
            if self._token is None:
                self._token = await self._login(http)
        return {"Authorization": f"Bearer {self._token}"}

    async def _login(self, http: httpx.AsyncClient) -> str:
        try:
            resp = await http.post(
                "/api/v1/auth/login",
                data={"username": self._email, "password": self._password},
            )
        except httpx.HTTPError as exc:
            msg = f"cognee login transport failure: {exc}"
            raise CogneeUnavailableError(msg) from exc
        if resp.status_code != HTTPStatus.OK:
            msg = f"cognee login rejected (HTTP {resp.status_code}) for {self._email}"
            raise CogneeAuthError(msg, status=resp.status_code)
        token = resp.json().get("access_token")
        if not token:
            msg = "cognee login succeeded but returned no access_token"
            raise CogneeAuthError(msg)
        return str(token)


class CogneeCircuit:
    """Consecutive-failure circuit breaker: 5 failures -> open 120s -> half-open probe.

    Numbers inherited from the tuned cognee-memory plugin. ``clock`` is injectable so tests
    advance time without sleeping.
    """

    def __init__(
        self,
        threshold: int = 5,
        open_seconds: float = 120.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        """Start closed; ``clock`` is injectable so tests advance time without sleeping."""
        self._threshold = threshold
        self._open_seconds = open_seconds
        self._clock = clock
        self._failures = 0
        self._opened_at: float | None = None

    @property
    def state(self) -> str:
        """One of ``closed`` / ``open`` / ``half_open`` (probe window reached)."""
        if self._opened_at is None:
            return "closed"
        if self._clock() - self._opened_at >= self._open_seconds:
            return "half_open"
        return "open"

    def allow(self) -> bool:
        """Whether a request may proceed (closed, or the half-open probe)."""
        return self.state != "open"

    def record_success(self) -> None:
        """Reset to closed."""
        self._failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        """Count a failure; trip (or re-trip after a failed probe) at the threshold."""
        self._failures += 1
        if self._failures >= self._threshold or self._opened_at is not None:
            self._opened_at = self._clock()


class CogneeClient:
    """Thin async HTTP client with auth, circuit breaking, and idempotent-only retries.

    All parameters default from the environment; ``transport`` is injectable for tests
    (httpx.MockTransport). One instance is shared per process via :func:`get_client`.
    """

    def __init__(  # noqa: PLR0913 - env-backed knobs, all keyword-only with defaults
        self,
        *,
        url: str | None = None,
        auth: CogneeAuth | None = None,
        key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = DEFAULT_TIMEOUT_S,
        circuit: CogneeCircuit | None = None,
    ) -> None:
        """Resolve every unset parameter from the environment; the HTTP client is lazy."""
        self._url = url if url is not None else base_url()
        creds = credentials()
        self._auth = auth if auth is not None else (CogneeAuth(*creds) if creds else None)
        self._key = key if key is not None else api_key()
        self._transport = transport
        self._timeout = timeout
        self.circuit = circuit if circuit is not None else CogneeCircuit()
        self._http: httpx.AsyncClient | None = None

    @property
    def configured(self) -> bool:
        """True when a base URL and at least one auth mechanism are present."""
        return bool(self._url) and (self._auth is not None or self._key is not None)

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                base_url=self._url or "",
                transport=self._transport,
                timeout=self._timeout,
            )
        return self._http

    async def aclose(self) -> None:
        """Close the underlying HTTP client (idempotent)."""
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def request(  # noqa: PLR0913 - one explicit slot per httpx payload kind, all keyword-only
        self,
        method: str,
        path: str,
        *,
        idempotent: bool = False,
        json: dict | None = None,
        data: dict[str, str | list[str]] | None = None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        params: dict | None = None,
        raw: bool = False,
    ) -> Json:
        """Send one API request and return the decoded JSON body.

        ``idempotent=True`` (reads only) enables bounded retries on transport errors and
        5xx; writes are never blind-retried — a duplicated ``/add`` is worse than a failed
        one. A 401 under bearer auth triggers exactly one token refresh + resend.
        ``raw=True`` returns the body text verbatim instead of JSON-decoding.
        """
        if not self.configured:
            raise CogneeNotConfiguredError
        if not self.circuit.allow():
            msg = f"cognee circuit open (state={self.circuit.state}); backing off"
            raise CogneeUnavailableError(msg)
        attempts = IDEMPOTENT_ATTEMPTS if idempotent else 1
        last_exc: Exception | None = None
        for attempt in range(attempts):
            if attempt:
                await asyncio.sleep(RETRY_BACKOFF_S * attempt)
            try:
                resp = await self._send(method, path, json=json, data=data, files=files, params=params)
            except (CogneeAuthError, CogneeNotConfiguredError):
                raise  # auth/config failures are terminal, not retryable noise
            except (httpx.HTTPError, CogneeUnavailableError) as exc:
                self.circuit.record_failure()
                last_exc = exc
                continue
            if resp.status_code >= HTTPStatus.INTERNAL_SERVER_ERROR and attempt + 1 < attempts:
                self.circuit.record_failure()
                last_exc = CogneeError(f"cognee HTTP {resp.status_code} on {path}", status=resp.status_code)
                continue
            if not resp.is_success:
                self.circuit.record_failure()
                msg = f"cognee HTTP {resp.status_code} on {path}: {resp.text[:300]}"
                raise CogneeError(msg, status=resp.status_code)
            self.circuit.record_success()
            return self._decode_body(resp, raw=raw)
        msg = f"cognee unreachable after {attempts} attempt(s) on {path}: {last_exc}"
        raise CogneeUnavailableError(msg) from last_exc

    @staticmethod
    def _decode_body(resp: httpx.Response, *, raw: bool) -> Json:
        """Success-path body decode: verbatim text when ``raw``, else JSON (empty body -> None)."""
        if raw:
            return resp.text
        if not resp.content:
            return None
        return resp.json()

    async def _send(  # noqa: PLR0913 - forwards request()'s explicit payload slots verbatim
        self,
        method: str,
        path: str,
        *,
        json: dict | None,
        data: dict[str, str | list[str]] | None,
        files: list[tuple[str, tuple[str, bytes, str]]] | None,
        params: dict | None,
    ) -> httpx.Response:
        """One send, with auth header resolution and a single 401 bearer refresh."""
        http = self._ensure_http()

        async def _once(headers: dict[str, str]) -> httpx.Response:
            return await http.request(method, path, headers=headers, json=json, data=data, files=files, params=params)

        if self._key is not None and self._auth is None:
            return await _once({"X-Api-Key": self._key})
        if self._auth is not None:
            resp = await _once(await self._auth.header(http))
            if resp.status_code == HTTPStatus.UNAUTHORIZED:
                self._auth.invalidate()
                resp = await _once(await self._auth.header(http))
            return resp
        raise CogneeNotConfiguredError

    # ------------------------------------------------------------- typed helpers

    async def health(self) -> Json:
        """GET /health — cheap reachability probe (no auth required)."""
        return await self.request("GET", "/health", idempotent=True)

    async def datasets(self) -> list[dict]:
        """GET /api/v1/datasets — authenticated list of the caller's datasets."""
        out = await self.request("GET", "/api/v1/datasets", idempotent=True)
        return out if isinstance(out, list) else []

    async def dataset_status(self, dataset_id: str) -> Json:
        """GET /api/v1/datasets/status for one dataset id (the API rejects a bare name)."""
        return await self.request("GET", "/api/v1/datasets/status", params={"dataset": [dataset_id]}, idempotent=True)

    async def search(
        self,
        query: str,
        search_type: str,
        dataset: str | None,
        top_k: int,
        node_name: list[str] | None = None,
    ) -> Json:
        """POST /api/v1/search (camelCase payload per the live OpenAPI).

        ``node_name`` restricts results to those node-set tags (the belongs_to_set filter,
        wired as ``nodeName``); omitted entirely when not given so the search spans the whole
        dataset. Verified against the live pgvector deployment: both CHUNKS and
        GRAPH_COMPLETION honour it, so recall can fetch only its own digests.
        """
        payload: dict[str, Json] = {"query": query, "searchType": search_type, "topK": top_k}
        if dataset:
            payload["datasets"] = [dataset]
        if node_name:
            payload["nodeName"] = list(node_name)
        return await self.request("POST", "/api/v1/search", json=payload)

    async def add(
        self,
        items: list[str],
        dataset: str,
        node_set: list[str] | None,
        *,
        run_in_background: bool = True,
    ) -> Json:
        """POST /api/v1/add (multipart) — text items become in-memory .txt uploads."""
        fields: dict[str, str | list[str]] = {
            "datasetName": dataset,
            "run_in_background": "true" if run_in_background else "false",
        }
        if node_set:
            fields["node_set"] = list(node_set)  # list value -> repeated multipart field
        files = [("data", (f"item-{i}.txt", text.encode("utf-8"), "text/plain")) for i, text in enumerate(items)]
        return await self.request("POST", "/api/v1/add", data=fields, files=files)

    async def cognify(  # noqa: PLR0913 - mirrors the endpoint's tuning fields, keyword-only
        self,
        dataset: str,
        *,
        run_in_background: bool = True,
        data_per_batch: int | None = None,
        chunks_per_batch: int | None = None,
        ontology_key: str | None = None,
    ) -> Json:
        """POST /api/v1/cognify for one dataset."""
        payload: dict[str, Json] = {"datasets": [dataset], "runInBackground": run_in_background}
        if data_per_batch is not None:
            payload["dataPerBatch"] = data_per_batch
        if chunks_per_batch is not None:
            payload["chunksPerBatch"] = chunks_per_batch
        if ontology_key is not None:
            payload["ontology_key"] = [ontology_key]
        return await self.request("POST", "/api/v1/cognify", json=payload)

    async def ontology_exists(self, key: str) -> bool:
        """Whether ``key`` is present in ``GET /api/v1/ontologies`` (the idempotency check).

        The listing endpoint returns a dict mapping each ontology_key to its metadata;
        a non-dict/empty response defensively means the key is simply absent.
        """
        listing = await self.request("GET", "/api/v1/ontologies", idempotent=True)
        return isinstance(listing, dict) and key in listing

    async def upload_ontology(self, key: str, xml: str, description: str | None = None) -> Json:
        """POST /api/v1/ontologies (multipart, keyed — the server dedupes by ontology_key)."""
        fields: dict[str, str | list[str]] = {"ontology_key": key}
        if description:
            fields["description"] = description
        files = [("ontology_file", (f"{key}.owl", xml.encode("utf-8"), "application/rdf+xml"))]
        return await self.request("POST", "/api/v1/ontologies", data=fields, files=files)

    async def memify(
        self,
        dataset: str,
        *,
        node_name: list[str] | None = None,
        run_in_background: bool = True,
    ) -> Json:
        """POST /api/v1/memify — derived-memory pass over one dataset (write, single attempt).

        The live MemifyPayloadDTO takes ``nodeName`` (NOT node_set) to scope the pass;
        omitted entirely when not given so the server applies its own default tasks.
        """
        payload: dict[str, Json] = {"datasetName": dataset, "runInBackground": run_in_background}
        if node_name:
            payload["nodeName"] = list(node_name)
        return await self.request("POST", "/api/v1/memify", json=payload)

    async def update_data(self, items: list[str], node_set: list[str] | None = None) -> Json:
        """PATCH /api/v1/update (multipart, add-shaped body) — replace stored data in place."""
        fields: dict[str, str | list[str]] = {}
        if node_set:
            fields["node_set"] = list(node_set)  # list value -> repeated multipart field
        files = [("data", (f"item-{i}.txt", text.encode("utf-8"), "text/plain")) for i, text in enumerate(items)]
        return await self.request("PATCH", "/api/v1/update", data=fields, files=files)

    async def dataset_data(self, dataset_id: str) -> list[dict]:
        """GET /api/v1/datasets/{dataset_id}/data — the dataset's data items (idempotent read)."""
        out = await self.request("GET", f"/api/v1/datasets/{dataset_id}/data", idempotent=True)
        return out if isinstance(out, list) else []

    async def delete_data(self, dataset_id: str, data_id: str) -> Json:
        """DELETE /api/v1/datasets/{dataset_id}/data/{data_id} — remove one data item (write)."""
        return await self.request("DELETE", f"/api/v1/datasets/{dataset_id}/data/{data_id}")

    async def delete_dataset(self, dataset_id: str) -> Json:
        """DELETE /api/v1/datasets/{dataset_id} — remove a whole dataset (write, single attempt).

        Used to tear down disposable eval datasets so recall evals never pollute real memory.
        """
        return await self.request("DELETE", f"/api/v1/datasets/{dataset_id}")

    async def improve(
        self,
        dataset: str | None = None,
        *,
        build_global_context_index: bool = False,
        run_in_background: bool = True,
    ) -> Json:
        """POST /api/v1/improve — graph self-improvement pass (write, single attempt).

        camelCase fields per the live ImprovePayloadDTO (which extends the memify payload,
        so the dataset travels as ``datasetName``); omitted entirely when not given.
        """
        payload: dict[str, Json] = {
            "runInBackground": run_in_background,
            "buildGlobalContextIndex": build_global_context_index,
        }
        if dataset:
            payload["datasetName"] = dataset
        return await self.request("POST", "/api/v1/improve", json=payload)

    async def export_markdown(self, dataset_id: str) -> str:
        """GET /api/v1/activity/export/{dataset_id} — markdown audit/backup export (idempotent).

        The Paket-2 backup path: this deployment has no COGX endpoint, so the activity
        export is the only server-side dump. The body is read verbatim and handled
        bimodally: a raw markdown body passes through untouched, while a JSON-encoded
        string (FastAPI encoding a ``str`` return) is unwrapped.
        """
        body = await self.request("GET", f"/api/v1/activity/export/{dataset_id}", idempotent=True, raw=True)
        text = body if isinstance(body, str) else ""
        if text.lstrip().startswith('"'):
            try:
                parsed = _json.loads(text)
            except ValueError:
                return text
            if isinstance(parsed, str):
                return parsed
        return text


# One shared client per process, mirroring the `_serena` gateway singleton in server.py:
# tools and the (future) capture drain reuse the same token, circuit state, and connection pool.
_client: CogneeClient | None = None


def get_client() -> CogneeClient:
    """The process-wide shared client, built lazily from the environment."""
    global _client  # noqa: PLW0603 - deliberate module singleton, mirrors server._serena
    if _client is None:
        _client = CogneeClient()
    return _client


def reset_client() -> None:
    """Drop the singleton (tests; env changes)."""
    global _client  # noqa: PLW0603 - deliberate module singleton, mirrors server._serena
    _client = None
