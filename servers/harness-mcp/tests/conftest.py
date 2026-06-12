import subprocess
from pathlib import Path

import pytest


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture(autouse=True)
def isolated_harness_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ~/.harness to a per-test temp dir so tests never touch the real ~/.harness."""
    harness = tmp_path / ".harness"
    harness.mkdir()
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(harness))
    return harness


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A small, committed git repo used by the core tests."""
    _run(["git", "init", "-q"], tmp_path)
    _run(["git", "config", "user.email", "t@t.t"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "payment.py").write_text("def charge():\n    return 1\n")
    (tmp_path / "src" / "util.py").write_text("from .payment import charge\n\n\ndef helper():\n    return charge()\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_payment.py").write_text("def test_charge():\n    assert True\n")
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    (tmp_path / ".env").write_text("SECRET=AKIAABCDEFGHIJKLMNOP\n")
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-qm", "init"], tmp_path)
    return tmp_path
