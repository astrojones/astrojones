"""Safe subprocess execution: never ``shell=True``, always time-bounded, output truncated."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

DEFAULT_TIMEOUT = 20
MAX_OUTPUT_CHARS = 20_000


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


def which(tool: str) -> str | None:
    """Return the resolved path of an executable, or ``None`` if absent."""
    return shutil.which(tool)


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
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False)
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
