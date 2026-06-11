"""Smoke tests for deploy-status / deploy-logs (no network: --help + name detection)."""

import subprocess

from conftest import TOOLS_DIR, load_tool


def test_status_help_runs_standalone():
    proc = subprocess.run(
        ["python3", str(TOOLS_DIR / "deploy-status"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert "deploy" in proc.stdout.lower()


def test_logs_help_runs_standalone():
    proc = subprocess.run(
        ["python3", str(TOOLS_DIR / "deploy-logs"), "--help"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0
    assert "log" in proc.stdout.lower()


def test_status_repo_name_falls_back_to_dirname(tmp_path, monkeypatch):
    ds = load_tool("deploy-status")
    monkeypatch.chdir(tmp_path)
    assert ds.repo_name() == tmp_path.name
