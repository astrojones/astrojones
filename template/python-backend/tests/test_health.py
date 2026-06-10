"""Smoke test for the API."""

from fastapi.testclient import TestClient

from __REPO_PKG__.api import app

client = TestClient(app)


def test_health_returns_ok() -> None:
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
