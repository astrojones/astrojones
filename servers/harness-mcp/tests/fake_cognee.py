"""A canned in-process cognee server for client/mem tests (httpx.MockTransport).

The counterpart of ``fake_serena.py`` for the memory stack: no subprocess, no network —
an httpx transport whose handler speaks just enough of the live API (login, datasets,
search, add, cognify, ontologies, health) and records every request so tests can assert
ordering (serial-first cognify) and payload shape. Failure knobs mirror fake_serena's
canned/error/hang philosophy: expire tokens, fail transports, return 5xx.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

import httpx

BASE_URL = "https://fake-cognee.test"
EMAIL = "t@t.t"
PASSWORD = "pw"  # noqa: S105 - canned test credential


class FakeCognee:
    """Programmable fake of the remote cognee API, driven through httpx.MockTransport."""

    def __init__(self, datasets: list[str] | None = None) -> None:
        """Start with ``datasets`` pre-existing (name -> deterministic fake id)."""
        self.datasets: list[str] = list(datasets or [])
        self.ontologies: set[str] = set()
        self.requests: list[tuple[str, str, dict]] = []  # (method, path, payload-ish)
        self.logins = 0
        self._token_serial = 0
        self._valid_tokens: set[str] = set()
        # Failure knobs
        self.expire_token_once = False
        self.transport_failures = 0  # raise ConnectError for this many requests
        self.server_error_times = 0  # return 500 for this many requests

    def transport(self) -> httpx.MockTransport:
        """The transport to inject into CogneeClient."""
        return httpx.MockTransport(self._handle)

    def client_kwargs(self) -> dict:
        """Ready-to-splat kwargs for a CogneeClient wired to this fake."""
        from repo_agent_harness.cognee_client import CogneeAuth  # noqa: PLC0415 - test-only import

        return {
            "url": BASE_URL,
            "auth": CogneeAuth(EMAIL, PASSWORD),
            "transport": self.transport(),
        }

    # ------------------------------------------------------------------ handler

    def _record(self, request: httpx.Request, payload: dict) -> None:
        self.requests.append((request.method, request.url.path, payload))

    def _authed(self, request: httpx.Request) -> bool:
        auth = request.headers.get("Authorization", "")
        token = auth.removeprefix("Bearer ")
        if token in self._valid_tokens:
            if self.expire_token_once:
                self.expire_token_once = False
                self._valid_tokens.discard(token)
                return False
            return True
        return False

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.transport_failures > 0:
            self.transport_failures -= 1
            msg = "fake transport failure"
            raise httpx.ConnectError(msg, request=request)
        if self.server_error_times > 0:
            self.server_error_times -= 1
            self._record(request, {})
            return httpx.Response(500, json={"detail": "fake internal error"})
        path = request.url.path
        if path == "/api/v1/auth/login":
            return self._login(request)
        if path == "/health":
            self._record(request, {})
            return httpx.Response(200, json={"status": "ok"})
        if not self._authed(request):
            return httpx.Response(401, json={"detail": "Unauthorized"})
        return self._route_authed(request, path)

    def _route_authed(self, request: httpx.Request, path: str) -> httpx.Response:  # noqa: PLR0911 - one return per route is the readable shape
        if path == "/api/v1/datasets" and request.method == "GET":
            self._record(request, {})
            body = [{"name": n, "id": f"id-{n}"} for n in self.datasets]
            return httpx.Response(200, json=body)
        if path == "/api/v1/datasets/status":
            self._record(request, dict(request.url.params))
            queried = request.url.params.get("dataset")
            known_ids = {f"id-{n}" for n in self.datasets}
            if queried not in known_ids:
                # Mirrors the live API: /datasets/status takes a dataset ID, not a name.
                return httpx.Response(400, json={"detail": [{"msg": "Input should be a valid UUID"}]})
            return httpx.Response(200, json={"status": "DATASET_PROCESSING_COMPLETED"})
        if path == "/api/v1/search":
            payload = json.loads(request.content)
            self._record(request, payload)
            return httpx.Response(200, json=[{"text": f"canned:{payload.get('searchType')}"}])
        if path == "/api/v1/add":
            return self._add(request)
        if path == "/api/v1/cognify":
            payload = json.loads(request.content)
            self._record(request, payload)
            return httpx.Response(200, json={"status": "ok"})
        if path.startswith("/api/v1/ontologies/"):
            key = path.rsplit("/", 1)[-1]
            self._record(request, {"key": key})
            if key in self.ontologies:
                return httpx.Response(200, json={"ontology_key": key})
            return httpx.Response(404, json={"detail": "not found"})
        if path == "/api/v1/ontologies" and request.method == "POST":
            payload = self._form_payload(request)
            self._record(request, payload)
            self.ontologies.add(str(payload.get("ontology_key")))
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": f"unrouted: {request.method} {path}"})

    def _add(self, request: httpx.Request) -> httpx.Response:
        payload = self._form_payload(request)
        self._record(request, payload)
        name = payload.get("datasetName")
        if name and name not in self.datasets:
            self.datasets.append(str(name))
        return httpx.Response(200, json={"id": f"add-{len(self.requests)}"})

    def _login(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        self.logins += 1
        if form.get("username") != [EMAIL] or form.get("password") != [PASSWORD]:
            return httpx.Response(400, json={"detail": "LOGIN_BAD_CREDENTIALS"})
        self._token_serial += 1
        token = f"token-{self._token_serial}"
        self._valid_tokens.add(token)
        return httpx.Response(200, json={"access_token": token, "token_type": "bearer"})

    @staticmethod
    def _form_payload(request: httpx.Request) -> dict:
        """Decode enough of a multipart body to assert fields (values and repeated lists)."""
        body = request.content.decode("utf-8", errors="replace")
        out: dict[str, object] = {}
        for part in body.split("Content-Disposition: form-data; name=")[1:]:
            name = part.split('"', 2)[1]
            value = part.split("\r\n\r\n", 1)[1].split("\r\n--", 1)[0]
            if name in out:
                prior = out[name]
                out[name] = [*prior, value] if isinstance(prior, list) else [prior, value]
            else:
                out[name] = value
        return out
