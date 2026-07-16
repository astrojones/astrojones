"""Host-safety tests for the docker helper scripts.

``docker/test.sh`` is the container ENTRYPOINT: it mutates git state in /workspace/repo
(git init / add -A / commit). A host run once left it in the caller's cwd — where the mkdir
of /workspace fails — and it committed into the real checked-out repo. These tests drive the
REAL script as a subprocess and pin the guard: outside a container it must refuse loudly and
touch nothing.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TEST_SH = _REPO_ROOT / "docker" / "test.sh"
_IN_CONTAINER = Path("/.dockerenv").exists() or Path("/run/.containerenv").exists()


@pytest.mark.skipif(_IN_CONTAINER, reason="in-container run would re-enter the entrypoint under test")
def test_docker_test_sh_refuses_to_run_on_host(tmp_path: Path) -> None:
    if not _TEST_SH.is_file():
        pytest.skip("docker/test.sh not present (standalone package run)")
    env = {k: v for k, v in os.environ.items() if k not in {"CLAUDE_CODE_OAUTH_TOKEN", "ASTROJONES_TEST_IN_CONTAINER"}}
    out = subprocess.run(
        ["bash", str(_TEST_SH)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    # rc 1 alone is ambiguous (the final PASS/FAIL gate also exits 1); the guard's
    # signature is the redirect to run.sh on stderr plus an untouched cwd.
    assert out.returncode == 1, out.stderr
    assert "docker/run.sh" in out.stderr
    assert not (tmp_path / ".git").exists()
