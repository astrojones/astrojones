"""Tests for the deploy MCP tools.

The plugin's deploy validators (``agent/tools/deploy-validate``,
``deploy-status``, ``deploy-logs``) are stdlib-only Python scripts that run
on the app repo. The harness server re-exposes them as MCP tools so the
model (and the opencode plugin's translator) can invoke them without
spawning a subprocess from a tool wrapper.

The tools do real work: ``repo_deploy_validate`` parses docker-compose.yml,
the repo-root ``deployment.yml``, Dockerfile, and the deploy workflow. Nothing
is hardcoded to a single org or domain — the org is read from the repo's git
origin owner and the app host from the manifest's ingress. The tests build
small app repos (with a synthetic git origin) and assert findings.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from repo_agent_harness import server

# The default fixture's identity. Kept as constants (not baked into assertions)
# so the suite proves the tools follow the repo's own owner/domain, not a
# hardcoded ``astrojones``. A second identity (``ekd-akd``) is exercised below.
DEFAULT_OWNER = "astrojones"
DEFAULT_DOMAIN = "astrojones.de"


# ---------------------------------------------------------------------------
# Helpers: build a deployable repo for an arbitrary org/domain
# ---------------------------------------------------------------------------


def _set_origin(repo: Path, owner: str) -> None:
    """Give the repo a synthetic ssh git origin so owner derivation is deterministic."""
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", f"git@github.com:{owner}/{repo.name}.git"],
        check=True,
        capture_output=True,
    )


def _deploy_files(repo: Path, owner: str = DEFAULT_OWNER, domain: str = DEFAULT_DOMAIN) -> None:
    """Write the four deploy files (repo-root manifest) + a git origin for ``owner``.

    Uses the repo name as metadata.name and the image/host target so all four
    hard rules pass. The ingress host is ``<repo>.<domain>`` — the source the
    tools derive the app URL from.
    """
    name = repo.name
    _set_origin(repo, owner)
    wf = repo / ".github" / "workflows"
    wf.mkdir(parents=True, exist_ok=True)
    (wf / "deploy.yml").write_text(
        "name: deploy\n"
        "on:\n  push: { branches: [main] }\n"
        "jobs:\n  deploy:\n"
        f"    uses: {owner}/.github/.github/workflows/nuk-deploy.yml@main\n"
    )
    (repo / "docker-compose.yml").write_text(
        f'services:\n  web:\n    image: ghcr.io/{owner}/{name}:latest\n    expose:\n      - "8080"\n'
    )
    (repo / "deployment.yml").write_text(
        f"apiVersion: nuk/v1\n"
        f"kind: Deployment\n"
        f"metadata:\n  name: {name}\n"
        f"spec:\n  ingress:\n"
        f"    - host: {name}.{domain}\n      service: web\n      port: 8080\n"
    )
    (repo / "Dockerfile").write_text("FROM scratch\nEXPOSE 8080\n")


# ---------------------------------------------------------------------------
# Fixture: a minimal deployable app repo (default org/domain)
# ---------------------------------------------------------------------------


@pytest.fixture
def deployable_repo(repo: Path) -> Path:
    """The bare `repo` fixture plus the four deploy files in the right shape."""
    _deploy_files(repo)
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


def test_deploy_validate_non_astrojones_org(monkeypatch, repo):
    """The key acceptance criterion: a non-astrojones org/domain validates.

    A repo whose git origin owner is ``ekd-akd`` reading a repo-root
    deployment.yml must pass, with the image and workflow verified against the
    ``ekd-akd`` owner (not a hardcoded ``astrojones``).
    """
    _deploy_files(repo, owner="ekd-akd", domain="ekd-akd.de")
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is True
    codes = {f["code"] for f in result["findings"]}
    assert "manifest-name" in codes
    image_ok = [f for f in result["findings"] if f["code"] == "image" and f["level"] == "ok"]
    assert image_ok and "ghcr.io/ekd-akd/" in image_ok[0]["message"]
    workflow_ok = [f for f in result["findings"] if f["code"] == "workflow" and f["level"] == "ok"]
    assert workflow_ok


def test_deploy_validate_legacy_nuklaut_manifest(monkeypatch, repo):
    """A legacy .nuklaut/deployment.yml still validates, with a deprecation warning."""
    _deploy_files(repo, owner="ekd-akd", domain="ekd-akd.de")
    # Relocate the manifest to the deprecated path.
    (repo / ".nuklaut").mkdir()
    (repo / ".nuklaut" / "deployment.yml").write_text((repo / "deployment.yml").read_text())
    (repo / "deployment.yml").unlink()
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is True  # warnings are allowed
    deprecations = [f for f in result["findings"] if "deprecat" in f["message"].lower()]
    assert deprecations
    assert deprecations[0]["level"] == "warning"


def test_deploy_validate_detects_placeholder(monkeypatch, repo):
    """A literal __REPO_NAME__ in any file produces a placeholder error."""
    _deploy_files(repo)
    (repo / "README.md").write_text("deploying __REPO_NAME__ soon\n")
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    placeholders = [f for f in result["findings"] if f["code"] == "placeholder"]
    assert placeholders
    assert placeholders[0]["level"] == "error"


def test_deploy_validate_detects_three_segment_image(monkeypatch, repo):
    """An image path with three segments (repo/repo) is the most common mistake."""
    _deploy_files(repo)
    (repo / "docker-compose.yml").write_text(
        f'services:\n  web:\n    image: ghcr.io/{DEFAULT_OWNER}/repo/repo:latest\n    expose:\n      - "8080"\n'
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    image_errors = [f for f in result["findings"] if f["code"] == "image"]
    assert image_errors
    assert "three segments" in image_errors[0]["message"] or "two segments" in image_errors[0]["message"]


def test_deploy_validate_detects_ports_keyword(monkeypatch, repo):
    """`ports:` in compose is forbidden (Traefik routes internally)."""
    _deploy_files(repo)
    (repo / "docker-compose.yml").write_text(
        f'services:\n  web:\n    image: ghcr.io/{DEFAULT_OWNER}/{repo.name}:latest\n    ports:\n      - "8080:8080"\n'
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(f["code"] == "compose-forbidden" and "ports" in f["message"] for f in result["findings"])


def test_deploy_validate_detects_traefik_labels(monkeypatch, repo):
    """`traefik.*` labels are forbidden (nuk generates routing from spec.ingress)."""
    _deploy_files(repo)
    (repo / "docker-compose.yml").write_text(
        f"services:\n  web:\n    image: ghcr.io/{DEFAULT_OWNER}/{repo.name}:latest\n"
        '    labels:\n      traefik.enable: "true"\n    expose:\n      - "8080"\n'
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(f["code"] == "compose-forbidden" and "traefik" in f["message"] for f in result["findings"])


def test_deploy_validate_detects_container_name(monkeypatch, repo):
    """`container_name:` collides with nuk's per-deploy project naming."""
    _deploy_files(repo)
    (repo / "docker-compose.yml").write_text(
        f"services:\n  web:\n    image: ghcr.io/{DEFAULT_OWNER}/{repo.name}:latest\n"
        '    container_name: my-app\n    expose:\n      - "8080"\n'
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(f["code"] == "compose-forbidden" and "container_name" in f["message"] for f in result["findings"])


def test_deploy_validate_detects_wrong_manifest_name(monkeypatch, repo):
    """metadata.name must equal the repo name (runner + secrets path derive from it)."""
    _deploy_files(repo)
    (repo / "deployment.yml").write_text(
        "apiVersion: nuk/v1\nkind: Deployment\nmetadata:\n  name: wrong-name\n"
    )
    monkeypatch.chdir(repo)
    result = server.repo_deploy_validate()
    assert result["ok"] is False
    assert any(f["code"] == "manifest-name" and f["level"] == "error" for f in result["findings"])


def test_deploy_validate_detects_stale_workflow_ref(monkeypatch, repo):
    """deploy.yml must call the org reusable workflow (not a stale ref)."""
    _deploy_files(repo)
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

    The app url is derived from the manifest's ingress host and the image from
    the git origin owner — not from any hardcoded org/domain. Without `gh`
    authenticated the tool reports a structured error so the model can react;
    it does not crash. (Real auth is exercised in integration tests / CI.)
    """
    monkeypatch.chdir(deployable_repo)
    result = server.repo_deploy_status()
    assert result["repo"] == deployable_repo.name
    assert result["app_url"] == f"https://{deployable_repo.name}.{DEFAULT_DOMAIN}"
    assert result["image"] == f"ghcr.io/{DEFAULT_OWNER}/{deployable_repo.name}:latest"
    assert "runs" in result
    # gh will be either present (CI authed) or missing (no auth in test env).
    # The contract is structured output either way.
    if "error" in result:
        assert "gh" in result["error"]


def test_deploy_status_non_astrojones_org(monkeypatch, repo, capsys):
    """Status derives url + image from the repo's own owner/host, not astrojones."""
    _deploy_files(repo, owner="ekd-akd", domain="ekd-akd.de")
    monkeypatch.chdir(repo)
    result = server.repo_deploy_status()
    assert result["app_url"] == f"https://{repo.name}.ekd-akd.de"
    assert result["image"] == f"ghcr.io/ekd-akd/{repo.name}:latest"


def test_deploy_logs_accepts_run_id(deployable_repo, monkeypatch, capsys):
    """repo_deploy_logs accepts a run id and returns a structured response."""
    monkeypatch.chdir(deployable_repo)
    result = server.repo_deploy_logs(run_id="12345")
    assert result["repo"] == deployable_repo.name
    assert "logs" in result or "error" in result  # structured either way


# ---------------------------------------------------------------------------
# CLI <-> MCP error-shape parity (issue #5 M1)
# ---------------------------------------------------------------------------


_REAL_RUN = subprocess.run


def _gh_not_found(*args, **kwargs):
    """subprocess.run stub: raise FileNotFoundError only for the gh argv.

    Patching ``subprocess.run`` is global (all modules share the one module
    object), so git calls — used by ``git.repo_root`` and ``deploy._origin_parts``
    for repo/owner detection — must pass through to the real implementation.
    Only the gh argv simulates a host without ``gh`` installed.
    """
    argv = args[0] if args else kwargs.get("args", [])
    if argv and argv[0] == "gh":
        msg = "gh"
        raise FileNotFoundError(msg)
    return _REAL_RUN(*args, **kwargs)


def test_deploy_status_cli_mcp_parity_on_gh_missing(deployable_repo, monkeypatch):
    """CLI _deploy_status and MCP repo_deploy_status share an error shape."""
    from repo_agent_harness import cli
    from repo_agent_harness import deploy as deploy_mod

    monkeypatch.chdir(deployable_repo)
    monkeypatch.setattr(deploy_mod.subprocess, "run", _gh_not_found)

    cli_res = cli._deploy_status(5, str(deployable_repo))
    mcp_res = server.repo_deploy_status()
    assert set(cli_res) == set(mcp_res), (set(cli_res), set(mcp_res))
    assert "hint" in cli_res
    assert cli_res["app_url"] == mcp_res["app_url"]
    assert cli_res["image"] == mcp_res["image"]


def test_deploy_logs_cli_mcp_parity_on_gh_missing(deployable_repo, monkeypatch):
    """CLI _deploy_logs and MCP repo_deploy_logs share an error shape."""
    from repo_agent_harness import cli
    from repo_agent_harness import deploy as deploy_mod

    monkeypatch.chdir(deployable_repo)
    monkeypatch.setattr(deploy_mod.subprocess, "run", _gh_not_found)

    cli_res = cli._deploy_logs("12345", 200, str(deployable_repo))
    mcp_res = server.repo_deploy_logs(run_id="12345")
    assert set(cli_res) == set(mcp_res), (set(cli_res), set(mcp_res))
    assert "hint" in cli_res
