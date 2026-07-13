import os
import subprocess
from pathlib import Path

import pytest


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


# Env vars the harness reads at call time. When pytest runs inside a live harness session
# (Claude Code with the plugin active), the session's own runtime config leaks into the test
# process — e.g. REPO_AGENT_HARNESS_NO_SERENA_GATE=1 silently disables the capability gate and
# flips refusal tests into connect attempts. Strip the whole family; tests that need one set
# it explicitly via monkeypatch, which layers on top of this autouse fixture.
_LEAKY_ENV_PREFIXES = ("REPO_AGENT_HARNESS_", "COGNEE_")
_LEAKY_ENV_VARS = ("CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT")


@pytest.fixture(autouse=True)  # noqa: RUF076 - isolation must cover every test, opting in per-test defeats it
def isolated_harness_home(tmp_path: Path, monkeypatch) -> Path:
    """Redirect ~/.harness to a per-test temp dir and strip leaked harness/session env vars."""
    for name in list(os.environ):
        if name.startswith(_LEAKY_ENV_PREFIXES) or name in _LEAKY_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
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
