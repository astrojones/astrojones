"""Tests for template/_shared/agent/tools/deploy-validate."""

import json
import shutil
import subprocess
from pathlib import Path

from conftest import TEMPLATE_DIR, TOOLS_DIR, make_app


def run(dv, root: Path, *args: str) -> dict:
    code = dv.main(["--root", str(root), "--json", *args])
    report = dv.LAST_REPORT
    assert (code == 0) == report["ok"]
    return report


def codes(report: dict, level: str) -> set[str]:
    return {f["code"] for f in report["findings"] if f["level"] == level}


def test_valid_app_passes(dv, app):
    report = run(dv, app)
    assert report["ok"], report
    assert codes(report, "error") == set()


def test_leftover_placeholder_fails(dv, app):
    (app / "docker-compose.yml").write_text(
        (app / "docker-compose.yml").read_text().replace("demo-app", "__REPO_NAME__")
    )
    report = run(dv, app)
    assert "placeholder" in codes(report, "error")


def test_three_segment_image_fails(dv, app):
    text = (app / "docker-compose.yml").read_text()
    (app / "docker-compose.yml").write_text(
        text.replace(
            "ghcr.io/astrojones/demo-app:latest",
            "ghcr.io/astrojones/demo-app/demo-app:latest",
        )
    )
    report = run(dv, app)
    assert "image" in codes(report, "error")


def test_image_name_mismatch_fails(dv, app):
    text = (app / "docker-compose.yml").read_text()
    (app / "docker-compose.yml").write_text(
        text.replace("astrojones/demo-app:", "astrojones/other-app:")
    )
    report = run(dv, app)
    assert "image" in codes(report, "error")


def test_ports_key_fails(dv, app):
    text = (
        (app / "docker-compose.yml")
        .read_text()
        .replace('expose:\n      - "8080"', 'ports:\n      - "8080:8080"')
    )
    (app / "docker-compose.yml").write_text(text)
    report = run(dv, app)
    assert "compose-forbidden" in codes(report, "error")


def test_container_name_fails(dv, app):
    text = (
        (app / "docker-compose.yml")
        .read_text()
        .replace("restart:", "container_name: demo\n    restart:")
    )
    (app / "docker-compose.yml").write_text(text)
    report = run(dv, app)
    assert "compose-forbidden" in codes(report, "error")


def test_traefik_label_fails(dv, app):
    text = (
        app / "docker-compose.yml"
    ).read_text() + '    labels:\n      - "traefik.enable=true"\n'
    (app / "docker-compose.yml").write_text(text)
    report = run(dv, app)
    assert "compose-forbidden" in codes(report, "error")


def test_manifest_name_mismatch_fails(dv, app):
    text = (
        (app / ".nuklaut" / "deployment.yml")
        .read_text()
        .replace("name: demo-app", "name: wrong-name")
    )
    (app / ".nuklaut" / "deployment.yml").write_text(text)
    report = run(dv, app)
    assert "manifest-name" in codes(report, "error")


def test_missing_manifest_fails(dv, app):
    (app / ".nuklaut" / "deployment.yml").unlink()
    report = run(dv, app)
    assert "manifest" in codes(report, "error")


def test_ingress_unknown_service_fails(dv, app):
    text = (
        (app / ".nuklaut" / "deployment.yml")
        .read_text()
        .replace("service: web", "service: api")
    )
    (app / ".nuklaut" / "deployment.yml").write_text(text)
    report = run(dv, app)
    assert "ingress-service" in codes(report, "error")


def test_ingress_port_not_exposed_warns(dv, app):
    text = (app / "docker-compose.yml").read_text().replace('"8080"', '"9999"')
    (app / "docker-compose.yml").write_text(text)
    report = run(dv, app)
    assert report["ok"]
    assert "expose-port" in codes(report, "warning")


def test_stale_workflow_ref_fails(dv, app):
    wf = app / ".github" / "workflows" / "deploy.yml"
    wf.write_text(
        wf.read_text().replace(
            "astrojones/.github/.github/workflows/nuk-deploy.yml", "someorg/old.yml"
        )
    )
    report = run(dv, app)
    assert "workflow" in codes(report, "error")


def test_missing_dockerfile_fails(dv, app):
    (app / "Dockerfile").unlink()
    report = run(dv, app)
    assert "dockerfile" in codes(report, "error")


def test_flow_style_ingress_is_parsed(dv, app):
    manifest = """apiVersion: nuk/v1
kind: Deployment
metadata:
  name: demo-app
spec:
  ingress:
    - { host: demo-app.astrojones.de, service: api, port: 8000, path: /api }
    - { host: demo-app.astrojones.de, service: web, port: 8080, path: / }
"""
    (app / ".nuklaut" / "deployment.yml").write_text(manifest)
    report = run(dv, app)
    # api service does not exist in compose -> error proves the flow entries were parsed
    assert "ingress-service" in codes(report, "error")


def test_repo_name_override(dv, app):
    report = run(dv, app, "--repo", "other-name")
    assert "image" in codes(report, "error")
    assert "manifest-name" in codes(report, "error")


def test_real_python_template_passes(dv, tmp_path):
    """Integration guard: the shipped python template + _shared overlay must validate clean."""
    root = tmp_path / "real-app"
    shutil.copytree(TEMPLATE_DIR / "python-backend", root)
    shutil.copytree(TEMPLATE_DIR / "_shared", root, dirs_exist_ok=True)
    for path in root.rglob("*"):
        if path.is_file():
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            path.write_text(
                text.replace("__REPO_NAME__", "real-app").replace(
                    "__REPO_PKG__", "real_app"
                )
            )
    report = run(dv, root)
    assert report["ok"], report["findings"]


def test_real_node_template_passes(dv, tmp_path):
    root = tmp_path / "real-node"
    shutil.copytree(TEMPLATE_DIR / "node", root)
    shutil.copytree(TEMPLATE_DIR / "_shared", root, dirs_exist_ok=True)
    for path in root.rglob("*"):
        if path.is_file():
            try:
                text = path.read_text()
            except UnicodeDecodeError:
                continue
            path.write_text(text.replace("__REPO_NAME__", "real-node"))
    report = run(dv, root)
    assert report["ok"], report["findings"]


def test_cli_subprocess_json(app):
    """The script runs standalone under bare python3 and emits machine-readable JSON."""
    proc = subprocess.run(
        ["python3", str(TOOLS_DIR / "deploy-validate"), "--root", str(app), "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["ok"] is True


def test_make_app_helper_is_consistent(dv, tmp_path):
    """A differently-named app validates clean (no demo-app hardcoding in checks)."""
    root = make_app(tmp_path / "zwischen-uns")
    report = run(dv, root)
    assert report["ok"], report["findings"]
