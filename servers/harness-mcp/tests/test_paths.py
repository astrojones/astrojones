"""Tests for the paths module."""

from pathlib import Path

from repo_agent_harness import paths


def test_repo_id_is_stable(tmp_path):
    root = str(tmp_path)
    assert paths.repo_id(root) == paths.repo_id(root)


def test_repo_id_is_12_hex_chars(tmp_path):
    rid = paths.repo_id(str(tmp_path))
    assert len(rid) == 12
    assert all(c in "0123456789abcdef" for c in rid)


def test_repo_id_stable_across_symlinks(tmp_path):
    link = tmp_path.parent / "link_to_tmp"
    link.symlink_to(tmp_path)
    try:
        assert paths.repo_id(str(tmp_path)) == paths.repo_id(str(link))
    finally:
        link.unlink()


def test_harness_home_default(monkeypatch):
    monkeypatch.delenv("REPO_AGENT_HARNESS_HOME", raising=False)
    assert paths.harness_home() == Path.home() / ".harness"


def test_harness_home_override(monkeypatch, tmp_path):
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path))
    assert paths.harness_home() == tmp_path


def test_repo_state_dir_creates_directory(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path / "harness"))
    state = paths.repo_state_dir(str(tmp_path))
    assert state.is_dir()
    assert state.stat().st_mode & 0o777 == 0o700


def test_repo_state_dir_path_structure(tmp_path, monkeypatch):
    home = tmp_path / "harness"
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(home))
    state = paths.repo_state_dir(str(tmp_path))
    rid = paths.repo_id(str(tmp_path))
    assert state == home / "repos" / rid


def test_is_cognee_onboarded_false_before_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path / "harness"))
    assert paths.is_cognee_onboarded(str(tmp_path)) is False


def test_is_cognee_onboarded_true_after_mark(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path / "harness"))
    paths.mark_cognee_onboarded(str(tmp_path))
    assert paths.is_cognee_onboarded(str(tmp_path)) is True


def test_cognee_onboarded_file_path_structure(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path / "harness"))
    f = paths.cognee_onboarded_file(str(tmp_path))
    assert f == paths.repo_state_dir(str(tmp_path)) / "cognee_onboarded.json"


def test_mark_cognee_onboarded_writes_meta(tmp_path, monkeypatch):
    import json

    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path / "harness"))
    paths.mark_cognee_onboarded(str(tmp_path), dataset="ds1", ontology_key="ok1")
    data = json.loads(paths.cognee_onboarded_file(str(tmp_path)).read_text())
    assert data["dataset"] == "ds1"
    assert data["ontology_key"] == "ok1"
    assert isinstance(data["onboarded_at"], (int, float))


def test_is_cognee_onboarded_false_on_invalid_json(tmp_path, monkeypatch):
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path / "harness"))
    paths.cognee_onboarded_file(str(tmp_path)).write_text("{ not json")
    assert paths.is_cognee_onboarded(str(tmp_path)) is False
