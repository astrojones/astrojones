"""Safe subprocess execution: never ``shell=True``, always time-bounded, output truncated."""

from __future__ import annotations

import os
import selectors
import shutil
import signal
import subprocess
import time
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

DEFAULT_TIMEOUT = 20
MAX_OUTPUT_CHARS = 20_000
DEFAULT_MAX_LINES = 10_000


@dataclass
class Result:
    """Bounded result of a subprocess invocation."""

    code: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def ok(self) -> bool:
        """True when the process exited 0 and did not time out."""
        return self.code == 0 and not self.timed_out


def _child_env() -> dict[str, str]:
    """A copy of the environment with the harness's OWN virtualenv stripped out.

    The MCP server runs under ``uv run``, which exports ``VIRTUAL_ENV`` and prepends its
    ``.venv/bin`` to ``PATH``. If children inherited that, every tool the harness shells
    out for a target repo — pytest, ruff, ty — would resolve to the harness's venv and
    import the harness's *own* installed package instead of the target's, verifying the
    wrong code. Stripping the leak makes children bind to the target repo's environment.
    """
    env = dict(os.environ)
    venv = env.pop("VIRTUAL_ENV", None)
    for key in ("UV_PROJECT", "UV_ACTIVE"):
        env.pop(key, None)
    if venv:
        venv_bin = str(Path(venv) / "bin")
        env["PATH"] = os.pathsep.join(
            p for p in env.get("PATH", "").split(os.pathsep) if p and str(Path(p)) != venv_bin
        )
    return env


def which(tool: str) -> str | None:
    """Return the resolved path of an executable, or ``None`` if absent.

    Resolves against the *scrubbed* PATH (:func:`_child_env`), so the harness never
    detects a tool that lives only in its own virtualenv — detection matches the
    environment children actually run in.
    """
    return shutil.which(tool, path=_child_env().get("PATH"))


def truncate(text: str, max_chars: int = MAX_OUTPUT_CHARS) -> str:
    """Truncate text to max_chars, appending a summary of the dropped byte count."""
    if len(text) <= max_chars:
        return text
    keep = max(0, max_chars - 80)
    return text[:keep] + f"\n…[truncated {len(text) - keep} chars]"


def run(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> Result:
    """Run ``cmd`` (an argv list — never a shell string) and return a bounded ``Result``."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            start_new_session=True,
            env=_child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        return Result(124, truncate(out, max_chars), f"timed out after {timeout}s", True)
    except FileNotFoundError:
        return Result(127, "", f"command not found: {cmd[0]}", False)
    return Result(
        proc.returncode,
        truncate(proc.stdout, max_chars),
        truncate(proc.stderr, max_chars),
        False,
    )


_READ_CHUNK = 65536


def run_streaming(
    cmd: list[str],
    cwd: str | None = None,
    timeout: int = DEFAULT_TIMEOUT,
    max_lines: int = DEFAULT_MAX_LINES,
    max_chars: int = MAX_OUTPUT_CHARS,
) -> Result:
    """Run ``cmd`` draining stdout incrementally, stopping early once enough lines are seen.

    Unlike :func:`run` (which buffers the *entire* output before the caller sees a byte), this
    reads stdout in chunks under a selector-driven deadline and returns as soon as ``max_lines``
    candidate lines (or ``max_chars``) are collected — terminating the child early. ``stderr`` is
    sent to ``DEVNULL`` so the single open pipe can never fill and deadlock (the reason this is a
    dedicated runner, not a tweak to :func:`run`). The selector honours the remaining time budget
    every iteration, so a child that stops emitting *without* closing stdout still hits the
    deadline (``timed_out=True``, code 124) rather than hanging. Returns the same bounded
    ``Result`` contract as :func:`run`.
    """
    try:
        proc = subprocess.Popen(  # noqa: S603
            cmd,
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=_child_env(),
        )
    except FileNotFoundError:
        return Result(127, "", f"command not found: {cmd[0]}", False)

    lines: list[str] = []
    buf = b""
    total_chars = 0
    timed_out = False
    eof = False
    deadline = time.monotonic() + timeout
    assert proc.stdout is not None  # stdout=PIPE guarantees a stream  # noqa: S101
    fd = proc.stdout.fileno()
    selector = selectors.DefaultSelector()
    selector.register(fd, selectors.EVENT_READ)
    try:
        while len(lines) < max_lines and total_chars < max_chars:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                timed_out = True
                break
            if not selector.select(timeout=remaining):
                timed_out = True  # nothing readable before the deadline → child wedged
                break
            chunk = os.read(fd, _READ_CHUNK)
            if not chunk:
                eof = True
                break
            buf += chunk
            *complete, buf = buf.split(b"\n")
            for raw in complete:
                line = raw.decode("utf-8", "replace")
                lines.append(line)
                total_chars += len(line) + 1
                if len(lines) >= max_lines:
                    break
        if eof and buf:  # trailing line with no final newline, on a clean close
            lines.append(buf.decode("utf-8", "replace"))
    finally:
        selector.close()
        with suppress(OSError, ValueError):
            proc.stdout.close()
        _terminate(proc)

    stdout = truncate("\n".join(lines), max_chars)
    if timed_out:
        return Result(124, stdout, f"timed out after {timeout}s", True)
    code = proc.returncode if (eof and proc.returncode is not None) else 0
    return Result(code, stdout, "", False)


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort teardown so an early-stopped or wedged child is never left orphaned.

    The child leads its own process group (the spawn sites pass ``start_new_session=True``),
    so we signal the whole group rather than just the direct child; otherwise grandchildren
    it spawned survive the kill and orphan.
    """
    if proc.poll() is not None:
        return
    with suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        with suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=1)
