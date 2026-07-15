import os
import pathlib
import subprocess

from repo_agent_harness import git, paths


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

    original = pathlib.Path.cwd()
    try:
        os.chdir(str(repo))
        result = git.repo_root()
        assert result == str(repo)
    finally:
        os.chdir(original)


def test_repo_root_resolves_linked_worktree(repo, tmp_path):
    """Linked-worktree repo_root() returns the worktree root, not the main checkout (#28)."""
    wt = tmp_path / "linked-wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "wt-branch"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    # In a linked worktree, .git is a file pointing at the main repo's worktrees dir.
    assert (wt / ".git").is_file()

    result = git.repo_root(cwd=str(wt))
    assert result is not None
    assert os.path.realpath(result) == os.path.realpath(str(wt))
    assert os.path.realpath(result) != os.path.realpath(str(repo))


def test_repo_id_differs_per_worktree(repo, tmp_path):
    """Each linked worktree gets its own repo_id, isolating harness state per worktree (#28)."""
    wt = tmp_path / "linked-wt"
    subprocess.run(
        ["git", "worktree", "add", str(wt), "-b", "wt-branch"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    main_root = git.repo_root(cwd=str(repo))
    wt_root = git.repo_root(cwd=str(wt))
    assert main_root is not None
    assert wt_root is not None
    assert paths.repo_id(wt_root) != paths.repo_id(main_root)


def _add_submodule(repo, tmp_path):
    """Side repo committed as a submodule of `repo` at vendor/sub, with one tracked file."""
    src = tmp_path / "subsrc"
    src.mkdir()
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@t.t"],
        ["config", "user.name", "t"],
    ):
        subprocess.run(["git", *args], cwd=src, check=True, capture_output=True)
    (src / "inner.py").write_text("inner = 1\n")
    subprocess.run(["git", "add", "inner.py"], cwd=src, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "sub init"], cwd=src, check=True, capture_output=True)
    # local-path submodule clones need the file protocol re-enabled (off since git 2.38.1)
    subprocess.run(
        ["git", "-c", "protocol.file.allow=always", "submodule", "add", str(src), "vendor/sub"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    subprocess.run(["git", "commit", "-qm", "add submodule"], cwd=repo, check=True, capture_output=True)


def test_ls_files_recurses_into_submodules(repo, tmp_path):
    _add_submodule(repo, tmp_path)
    files = git.ls_files(str(repo))
    assert "vendor/sub/inner.py" in files
    assert "vendor/sub" not in files  # the gitlink entry is replaced by its contents


def test_list_files_recurses_into_submodules(repo, tmp_path):
    _add_submodule(repo, tmp_path)
    (repo / "scratch.py").write_text("x = 1\n")
    files = git.list_files(str(repo))
    assert "vendor/sub/inner.py" in files
    assert "scratch.py" in files  # untracked listing still works alongside recursion
