import pathlib

from harness import git


def test_repo_root(repo):
    assert git.repo_root(str(repo)) == str(repo)


def test_status_clean(repo):
    st = git.status(str(repo))
    assert st["dirty"] is False
    assert st["branch"]


def test_status_dirty(repo):
    (repo / "src" / "util.py").write_text("changed = 1\n")
    st = git.status(str(repo))
    assert st["dirty"] is True
    assert "src/util.py" in st["changed_files"]


def test_diff_current_redacts(repo):
    (repo / "src" / "util.py").write_text("token = 'AKIAABCDEFGHIJKLMNOP'\n")
    d = git.diff_current(str(repo))
    assert "AKIA" not in d["diff"]
    assert "[REDACTED]" in d["diff"]


def test_repo_root_uses_claude_project_dir(monkeypatch, repo):
    """CLAUDE_PROJECT_DIR env var seeds repo_root() when cwd is None."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(repo))
    result = git.repo_root()
    assert result == str(repo)


def test_repo_root_ignores_claude_project_dir_when_cwd_explicit(monkeypatch, repo):
    """Explicit cwd arg takes priority over CLAUDE_PROJECT_DIR."""
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", "/nonexistent_no_such_dir_xyz")
    result = git.repo_root(cwd=str(repo))
    assert result == str(repo)


def test_repo_root_falls_back_to_process_cwd_when_env_unset(monkeypatch, repo):
    """With no env var and no cwd, falls back to process cwd (shell.run default)."""
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    import os

    original = pathlib.Path.cwd()
    try:
        os.chdir(str(repo))
        result = git.repo_root()
        assert result == str(repo)
    finally:
        os.chdir(original)
