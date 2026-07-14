"""Tests for shell.run_streaming — incremental, deadline-bounded, early-stopping output drain."""

import os
import sys
import textwrap
import time

import pytest
from repo_agent_harness import shell


def _py_child(body: str) -> list[str]:
    """Build an argv that runs ``body`` in a fresh Python child (unbuffered stdout)."""
    return [sys.executable, "-u", "-c", textwrap.dedent(body)]


@pytest.mark.timeout(15)
def test_run_streaming_early_stops():
    # A child that floods stdout must be cut off once max_lines candidates are drained,
    # returning fast without timing out and without buffering the whole flood.
    cmd = _py_child(
        """
        import sys
        for i in range(100000):
            sys.stdout.write(f"line{i}\\n")
        """
    )
    res = shell.run_streaming(cmd, max_lines=5, timeout=10)
    assert res.timed_out is False
    assert res.stdout.count("\n") <= 50, "did not stop early — buffered the whole flood"
    assert "line0" in res.stdout


@pytest.mark.timeout(5)
def test_run_streaming_deadline_when_child_hangs():
    # The deterministic hang: the child emits 3 lines then sleeps forever without EOF.
    # A plain blocking readline loop would hang here; the selector deadline must fire at ~1s.
    cmd = _py_child(
        """
        import sys, time
        for i in range(3):
            sys.stdout.write(f"partial{i}\\n")
        sys.stdout.flush()
        time.sleep(3600)
        """
    )
    res = shell.run_streaming(cmd, max_lines=100, timeout=1)
    assert res.timed_out is True
    assert res.code == 124
    assert "partial0" in res.stdout


@pytest.mark.timeout(15)
def test_run_streaming_no_stderr_deadlock():
    # Flooding BOTH stdout and stderr must not deadlock: stderr is DEVNULL (single pipe),
    # so an unread stderr pipe can never fill and wedge the child.
    cmd = _py_child(
        """
        import sys
        for i in range(50000):
            sys.stdout.write(f"out{i}\\n")
            sys.stderr.write(f"err{i}\\n")
        """
    )
    res = shell.run_streaming(cmd, max_lines=10, timeout=10)
    assert res.timed_out is False
    assert "out0" in res.stdout


@pytest.mark.timeout(15)
def test_run_streaming_command_not_found():
    res = shell.run_streaming(["definitely-not-a-real-binary-xyz"], max_lines=5, timeout=5)
    assert res.code == 127


@pytest.mark.timeout(15)
def test_run_streaming_completes_under_limit():
    # A short, clean child whose output is below max_lines returns all of it, exit 0.
    cmd = _py_child(
        """
        for i in range(3):
            print(f"row{i}")
        """
    )
    res = shell.run_streaming(cmd, max_lines=100, timeout=10)
    assert res.timed_out is False
    assert res.code == 0
    assert ["row0", "row1", "row2"] == res.stdout.splitlines()


@pytest.mark.timeout(10)
def test_run_streaming_kills_orphan_grandchild():
    # A wedged child that has itself spawned a grandchild must not leave the grandchild
    # orphaned on teardown. The child spawns a sleeping grandchild, prints its PID, then
    # both block forever (no EOF) → run_streaming hits the deadline and _terminate must
    # signal the whole process group, reaping the grandchild too.
    cmd = _py_child(
        """
        import subprocess, sys, time
        gc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(3600)"])
        sys.stdout.write(f"GRANDCHILD={gc.pid}\\n")
        sys.stdout.flush()
        time.sleep(3600)
        """
    )
    res = shell.run_streaming(cmd, max_lines=100, timeout=1)
    assert res.timed_out is True
    pid_line = next(line for line in res.stdout.splitlines() if line.startswith("GRANDCHILD="))
    grandchild_pid = int(pid_line.split("=", 1)[1])

    # Give the group-kill a beat to propagate, then the grandchild must be gone.
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    with pytest.raises(ProcessLookupError):
        os.kill(grandchild_pid, 0)


def test_child_env_strips_harness_venv(monkeypatch):
    """The harness's own venv (VIRTUAL_ENV + its bin on PATH) must not reach children."""
    venv = "/opt/harness/.venv"
    monkeypatch.setenv("VIRTUAL_ENV", venv)
    monkeypatch.setenv("UV_PROJECT", "/opt/harness")
    monkeypatch.setenv("PATH", os.pathsep.join([venv + "/bin", "/usr/bin", "/bin"]))
    env = shell._child_env()
    assert "VIRTUAL_ENV" not in env
    assert "UV_PROJECT" not in env
    path_parts = env["PATH"].split(os.pathsep)
    assert venv + "/bin" not in path_parts
    assert "/usr/bin" in path_parts and "/bin" in path_parts


def test_run_does_not_leak_virtual_env(monkeypatch):
    """A spawned child sees no inherited VIRTUAL_ENV, so tools bind to the target env."""
    monkeypatch.setenv("VIRTUAL_ENV", "/opt/harness/.venv")
    res = shell.run([sys.executable, "-c", "import os; print(os.environ.get('VIRTUAL_ENV', 'UNSET'))"])
    assert res.stdout.strip() == "UNSET"
