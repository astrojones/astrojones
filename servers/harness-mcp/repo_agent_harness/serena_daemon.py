"""One Serena HTTP daemon per worktree: discovery, identity check, detached spawn, stop.

The structural fix for the Serena process multiplication (June wedges, ~492MB duplication):
every harness process used to own a stdio child; now the FIRST process to need Serena spawns
ONE detached ``streamable-http`` daemon for the worktree and every process — main session and
all subagents — connects to it as an HTTP client. Serena's non-stdio lifespan keeps its agent
singleton alive across connections, so the daemon natively serves many clients.

Discovery is port-derived (a stable hash of the worktree path) with a small linear probe for
squatted ports, and **identity is verified, never assumed**: a port answering MCP is only
"ours" if its ``get_current_config`` names this worktree's project (``serverInfo.name`` is
just "Serena" — useless for identity). ``daemon.json`` under the repo state dir records
{port, pid, argv} for introspection and the stop CLI; it is never trusted for liveness.

No idle-reap machinery in v1 (decided): the daemon outlives harness processes by design and
is stopped explicitly via ``repo-agent-harness serena-daemon stop``; dead-PID state files are
cleaned opportunistically at server startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import signal
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from repo_agent_harness import paths

if TYPE_CHECKING:
    from typing import Literal

LOG = logging.getLogger(__name__)

SERENA_TRANSPORT_ENV = "REPO_AGENT_HARNESS_SERENA_TRANSPORT"
_DEFAULT_TRANSPORT = "http"

_PORT_BASE = 20000
_PORT_SPAN = 10000
_PROBE_PORTS = 5  # base plus linear probe for squatted ports
_PROBE_TIMEOUT_S = 5.0
_POLL_INTERVAL_S = 0.5
_STOP_GRACE_S = 3.0

_STATE_NAME = "daemon.json"
_LOG_NAME = "daemon.log"

# Daemons spawned by THIS process: kept so a dead child can be reaped (poll()) — otherwise it
# lingers as a zombie that os.kill(pid, 0) reports alive, fooling every liveness check until
# the spawner exits. Other processes' daemons are not our children and need no reaping.
_SPAWNED: dict[int, subprocess.Popen] = {}


def _reap_own(pid: int) -> None:
    """Reap ``pid`` if it is a dead child of this process (no-op otherwise)."""
    proc = _SPAWNED.get(pid)
    if proc is not None and proc.poll() is not None:
        _SPAWNED.pop(pid, None)


def serena_transport() -> str:
    """The configured Serena transport: ``http`` (default) or ``stdio`` (fallback)."""
    raw = (os.environ.get(SERENA_TRANSPORT_ENV) or _DEFAULT_TRANSPORT).strip().lower()
    return raw if raw in {"http", "stdio"} else _DEFAULT_TRANSPORT


def base_port(root: str) -> int:
    """The worktree's deterministic daemon port (stable hash of the resolved path)."""
    return _PORT_BASE + int(paths.repo_id(root), 16) % _PORT_SPAN


def daemon_url(port: int) -> str:
    """The MCP endpoint URL for a daemon on ``port`` (fastmcp streamable-http default path)."""
    return f"http://127.0.0.1:{port}/mcp"


def _state_dir(root: str) -> Path:
    d = paths.repo_state_dir(root) / "serena"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file(root: str) -> Path:
    """Path of the introspection record ({port, pid, argv}); never trusted for liveness."""
    return _state_dir(root) / _STATE_NAME


def read_state(root: str) -> dict | None:
    """The recorded daemon state, or None when absent/unreadable."""
    try:
        data = json.loads(state_file(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_state(root: str, port: int, pid: int, argv: list[str]) -> None:
    with contextlib.suppress(OSError):
        state_file(root).write_text(
            json.dumps({"port": port, "pid": pid, "argv": argv, "started_at": time.time()}),
            encoding="utf-8",
        )


def clean_stale_state(root: str) -> bool:
    """Drop a daemon.json whose PID is no longer alive (returns whether it was removed)."""
    state = read_state(root)
    if state is None:
        return False
    pid = state.get("pid")
    if isinstance(pid, int):
        _reap_own(pid)
        try:
            os.kill(pid, 0)
        except OSError:
            pass  # dead — fall through to removal
        else:
            return False  # alive — keep the record
    with contextlib.suppress(OSError):
        state_file(root).unlink()
    return True


def identity_marker(root: str) -> str:
    """The substring a daemon's ``initial_instructions`` must contain to be THIS worktree's.

    Serena's instructions state ``The project with name '<name>' at <path> is activated.`` —
    the *path* is matched (unique per worktree), not the name (project.yml names drift across
    clones). ``get_current_config`` would be the natural identity call, but the claude-code
    context does not register it; ``initial_instructions`` is always served.
    """
    return f"at {Path(root).resolve()} is activated"


def daemon_args(root: str, port: int) -> list[str]:
    """The argv (after the executable) to start the worktree's Serena HTTP daemon."""
    return [
        "start-mcp-server",
        "--context",
        "claude-code",
        "--project",
        root,
        "--transport",
        "streamable-http",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--enable-web-dashboard",
        "false",
        "--enable-gui-log-window",
        "false",
    ]


def _is_connection_refused(exc: BaseException) -> bool:
    """Whether ``exc``'s cause/context/group chain contains a connection-refused transport error.

    fastmcp wraps the raw httpx/OS error in its own connect exception (and anyio task groups
    in ``BaseExceptionGroup``s), so the refusal has to be found by walking, not by type of the
    surface exception.
    """
    stack: list[BaseException] = [exc]
    seen: set[int] = set()
    while stack:
        current = stack.pop()
        if id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, httpx.ConnectError | ConnectionRefusedError):
            return True
        if isinstance(current, BaseExceptionGroup):
            stack.extend(current.exceptions)
        stack.extend(linked for linked in (current.__cause__, current.__context__) if linked is not None)
    return False


async def _probe(port: int, marker: str) -> Literal["ours", "other", "free"]:
    """Classify what answers on ``port``: our daemon, something else, or nothing at all.

    Identity, not liveness: a full MCP handshake plus ``initial_instructions``, whose text
    names the active project's path (see :func:`identity_marker`). Connection refused ⇒
    ``free`` (a spawn candidate); anything answering that is not verifiably ours — wrong
    project, non-MCP service, a daemon still booting its handshake — ⇒ ``other``.
    """
    client = Client(StreamableHttpTransport(daemon_url(port)))
    try:
        with anyio.fail_after(_PROBE_TIMEOUT_S):
            async with client:
                result = await client.call_tool_mcp("initial_instructions", {})
    except Exception as exc:  # noqa: BLE001 - classification, not handling
        return "free" if _is_connection_refused(exc) else "other"
    if result.isError:
        return "other"
    text = "".join(getattr(c, "text", "") or "" for c in result.content)
    return "ours" if marker in text else "other"


def _spawn(root: str, port: int, command: str) -> int:
    """Start the detached daemon (own session — it must outlive this process). Returns the PID."""
    argv = [command, *daemon_args(root, port)]
    log_path = _state_dir(root) / _LOG_NAME
    with log_path.open("ab") as log_file:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv from our own config, no shell
            argv,
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
        )
    _SPAWNED[proc.pid] = proc
    _write_state(root, port=port, pid=proc.pid, argv=argv)
    LOG.info("spawned serena daemon pid=%s port=%s for %s", proc.pid, port, root)
    return proc.pid


async def ensure_daemon(root: str, command: str, budget: float) -> str:
    """Find or start the worktree's daemon and return its URL once it verifiably answers.

    Probe the deterministic port (plus a linear window for squatters); connect to an existing
    daemon whose identity matches; otherwise spawn detached on the first free port and poll
    until the identity check passes — a cold daemon boots one language server per seeded
    language, hence the generous ``budget`` (the connect budget). An EADDRINUSE race (two
    processes spawning simultaneously — normally prevented by the connect flock) resolves
    itself: the loser's process dies, the poll finds the winner answering with the right
    identity. Raises TimeoutError when nothing verifiably ours answers within the budget.
    """
    marker = identity_marker(root)
    ports = range(base_port(root), base_port(root) + _PROBE_PORTS)
    spawn_port: int | None = None
    for port in ports:
        verdict = await _probe(port, marker)
        if verdict == "ours":
            return daemon_url(port)
        if verdict == "free" and spawn_port is None:
            spawn_port = port
    if spawn_port is None:
        msg = f"no free port for the serena daemon in {ports.start}-{ports.stop - 1} (all squatted)"
        raise TimeoutError(msg)
    _spawn(root, spawn_port, command)
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if await _probe(spawn_port, marker) == "ours":
            return daemon_url(spawn_port)
        await asyncio.sleep(_POLL_INTERVAL_S)
    msg = f"serena daemon on port {spawn_port} did not become ready within {budget}s"
    raise TimeoutError(msg)


def stop_daemon(root: str) -> dict:
    """Stop the worktree's daemon (the explicit v1 lifecycle lever) and clear its state file.

    SIGTERM to the daemon's process group (it owns its session), escalating to SIGKILL after
    a short grace. Safe when nothing is running — reports ``stopped: False``.
    """
    state = read_state(root)
    pid_raw = state.get("pid") if state is not None else None
    if not isinstance(pid_raw, int):
        return {"ok": True, "stopped": False, "reason": "no daemon state recorded"}
    pid = pid_raw
    stopped = False
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = None  # already gone
    if pgid is not None:
        with contextlib.suppress(OSError):
            os.killpg(pgid, signal.SIGTERM)
        deadline = time.monotonic() + _STOP_GRACE_S
        while time.monotonic() < deadline:
            _reap_own(pid)  # a dead child must be reaped, or the zombie reads as alive below
            try:
                os.kill(pid, 0)
            except OSError:
                break
            time.sleep(0.1)
        else:
            with contextlib.suppress(OSError):
                os.killpg(pgid, signal.SIGKILL)
            _reap_own(pid)
        stopped = True
    with contextlib.suppress(OSError):
        state_file(root).unlink()
    return {"ok": True, "stopped": stopped, "pid": pid, "port": state.get("port")}
