"""Tests for the deploy MCP tools.

The plugin's deploy validators (``agent/tools/deploy-validate``,
``deploy-status``, ``deploy-logs``) are stdlib-only Python scripts that run
on the app repo. The harness server re-exposes them as MCP tools so the
model (and the opencode plugin's translator) can invoke them without
spawning a subprocess from a tool wrapper.

The tools do real work: ``repo_deploy_validate`` parses docker-compose.yml,
.nuklaut/deployment.yml, Dockerfile, and the deploy workflow. The tests
build small app repos with the four deploy files and assert findings.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from repo_agent_harness import server

# ---------------------------------------------------------------------------
# Fixture: a minimal deployable astrojones app repo
# ---------------------------------------------------------------------------


@pytest.fixture
def deployable_repo(repo: Path) -> Path:
    """The bare `repo` fixture plus the four deploy files in the right shape.

    Uses the repo name (from conftest's fixture) as the metadata.name and
    the image target so all four hard rules pass.
    """
    name = repo.name
    # .github/workflows/deploy.yml
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "name: deploy\n"
        "on:\n  push: { branches: [main] }\n"
        "jobs:\n  deploy:\n"
        "    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    # docker-compose.yml
    (repo / "docker-compose.yml").write_text(
        f"services:\n  web:\n    image: ghcr.io/astrojones/{name}:latest\n    expose:\n      - \"8080\"\n"
    )
    # .nuklaut/deployment.yml
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text(
        f"apiVersion: nuk/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n  name: {name}\n"
        f"spec:\n  ingress:\n"
        f"    - host: {name}.astrojones.de\n      service: web\n      port: 8080\n"
    )
    # Dockerfile
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")
    return repo


# ---------------------------------------------------------------------------
# repo_deploy_validate
# ---------------------------------------------------------------------------


def test_deploy_validate_clean_repo_is_ok(deployable_repo, monkeypatch):
    """All four hard rules pass on a clean repo."""
    monkeypatch.chdir(deployable_repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is True
    codes = {f["code"] for f in result["findings"]}
    assert "placeholder" in codes
    assert "manifest-name" in codes
    assert "workflow" in codes
    assert any(f["level"] == "ok" for f in result["findings"])


def test_deploy_validate_detects_placeholder(monkeypatch, repo):
    """A literal __REPO_NAME__ in any file produces a placeholder error."""
    (repo / "docker-compose.yml").write_text("__REPO_NAME__: placeholder leak\n")
    (repo / "Dockerfile").write_text("FROM scratch\n")
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text("apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: x\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "jobs:\n  deploy:\n"
        "    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    placeholders = [f for f in result["findings"] if f["code"] == "placeholder"]
    assert placeholders
    assert placeholders[0]["level"] == "error"


def test_deploy_validate_detects_three_segment_image(monkeypatch, repo):
    """An image path with three segments (repo/repo) is the most common mistake."""
    (repo / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: ghcr.io/astrojones/repo/repo:latest\n    expose:\n      - \"8080\"\n"
    )
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text(
        f"apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: {repo.name}\n"
    )
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "jobs:\n  deploy:\n"
        "    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    image_errors = [f for f in result["findings"] if f["code"] == "image"]
    assert image_errors
    assert "three segments" in image_errors[0]["message"] or "two segments" in image_errors[0]["message"]


def test_deploy_validate_detects_ports_keyword(monkeypatch, repo):
    """`ports:` in compose is forbidden (Traefik routes internally)."""
    (repo / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: ghcr.io/astrojones/x:latest\n    ports:\n      - \"8080:8080\"\n"
    )
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text("apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: x\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "jobs:\n  deploy:\n"
        "    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(
        f["code"] == "compose-forbidden" and "ports" in f["message"]
        for f in result["findings"]
    )


def test_deploy_validate_detects_traefik_labels(monkeypatch, repo):
    """`traefik.*` labels are forbidden (nuk generates routing from spec.ingress)."""
    (repo / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: ghcr.io/astrojones/x:latest\n"
        "    labels:\n      traefik.enable: \"true\"\n    expose:\n      - \"8080\"\n"
    )
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text("apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: x\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "jobs:\n  deploy:\n"
        "    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(
        f["code"] == "compose-forbidden" and "traefik" in f["message"]
        for f in result["findings"]
    )


def test_deploy_validate_detects_container_name(monkeypatch, repo):
    """`container_name:` collides with nuk's per-deploy project naming."""
    (repo / "docker-compose.yml").write_text(
        "services:\n  web:\n    image: ghcr.io/astrojones/x:latest\n"
        "    container_name: my-app\n    expose:\n      - \"8080\"\n"
    )
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text("apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: x\n")
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "jobs:\n  deploy:\n"
        "    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(
        f["code"] == "compose-forbidden" and "container_name" in f["message"]
        for f in result["findings"]
    )


def test_deploy_validate_detects_wrong_manifest_name(monkeypatch, repo):
    """metadata.name must equal the repo name (runner + secrets path derive from it)."""
    (repo / "docker-compose.yml").write_text(
        f"services:\n  web:\n    image: ghcr.io/astrojones/{repo.name}:latest\n    expose:\n      - \"8080\"\n"
    )
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text(
        "apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: wrong-name\n"
    )
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "jobs:\n  deploy:\n"
        "    uses: astrojones/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(
        f["code"] == "manifest-name" and f["level"] == "error"
        for f in result["findings"]
    )


def test_deploy_validate_detects_stale_workflow_ref(monkeypatch, repo):
    """deploy.yml must call the org reusable workflow (not a stale ref)."""
    (repo / "docker-compose.yml").write_text(
        f"services:\n  web:\n    image: ghcr.io/astrojones/{repo.name}:latest\n    expose:\n      - \"8080\"\n"
    )
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text(
        f"apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: {repo.name}\n"
    )
    (repo / ".github" / "workflows").mkdir(parents=True)
    (repo / ".github" / "workflows" / "deploy.yml").write_text(
        "jobs:\n  deploy:\n    uses: someone-else/their-workflow@v1\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(f["code"] == "workflow" and f["level"] == "error" for f in result["findings"])


def test_deploy_validate_outside_repo(tmp_path, monkeypatch):
    """Outside a git repo, the tool returns the standard no-repo error."""
    monkeypatch.chdir(tmp_path)
    result = server.repo_deploy_validate()
    assert "error" in result


# ---------------------------------------------------------------------------
# repo_deploy_status and repo_deploy_logs — thin gh wrappers
# ---------------------------------------------------------------------------


def test_deploy_status_returns_structured_info(deployable_repo, monkeypatch, capsys):
    """repo_deploy_status reports the app url, image, and recent runs.

    Without `gh` authenticated the tool reports a structured error so the
    model can react; it does not crash. (Real auth is exercised in
    integration tests / CI, not in unit tests.)
    """
    monkeypatch.chdir(deployable_repo)
    result = server.repo_deploy_status()
    assert result["repo"] == deployable_repo.name
    assert result["app_url"] == f"https://{deployable_repo.name}.astrojones.de"
    assert result["image"] == f"ghcr.io/astrojones/{deployable_repo.name}:latest"
    assert "runs" in result
    # gh will be either present (CI authed) or missing (no auth in test env).
    # The contract is structured output either way.
    if "error" in result:
        assert "gh" in result["error"]


def test_deploy_logs_accepts_run_id(deployable_repo, monkeypatch, capsys):
    """repo_deploy_logs accepts a run id and returns a structured response."""
    monkeypatch.chdir(deployable_repo)
    result = server.repo_deploy_logs(run_id="12345")
    assert result["repo"] == deployable_repo.name
    assert "logs" in result or "error" in result  # structured either way
