"""Optional local cognee: one host-singleton Docker container as a fallback for the remote.

The deliberate sibling of ``serena_daemon.py``. Where Serena runs one HTTP daemon *per
worktree* (path-hashed port), local cognee is a single *per-host* container — durable memory
is cross-repo (default dataset ``agent_sessions``), so it uses a fixed name/port/volume, not a
path-hashed one. It boots the exact REST API ``cognee_client`` already speaks (port 8000), so
the client points at it unchanged once ``endpoint.json`` exists.

Trigger for the AUTO path: the harness has no *remote* configured (``COGNEE_BASE_URL`` unset)
AND auto-start is armed (``COGNEE_LOCAL_ENABLE=1``) — opt-in so a server that merely failed to
inherit an existing remote never boots a split-brain second store (Docker absent still just
stays unconfigured, a clearer hint, never a crash). The explicit ``cognee-local up`` CLI brings
local up regardless of the flag.

Embeddings/LLM are configured to match the remote deployment exactly (OpenRouter
``openai/text-embedding-3-small`` at 1536 dims), so vectors written locally and remotely share
one embedding space — data is interchangeable, and recall works the moment an OpenRouter key is
present in the environment. The key is passed through from the environment at ``docker run``
time and never persisted in ``endpoint.json``.

The image tag and the summarize-prompt override are also kept in lockstep with the remote
deployment (astrojones/cognee, ``docker-compose.yml``) by hand — bump ``_DEFAULT_IMAGE`` and
re-copy ``cognee_local_summarize_prompt.txt`` from that repo's ``prompts/summarize_content.txt``
whenever the remote changes either. The prompt override exists because cognee's stock summarizer
invents facts to fill a required field when a chunk has no substantive content — read-only
bind-mounted over the in-image prompt so an empty/near-empty chunk (e.g. a fresh "Got it."-style
digest) can't get hallucinated into a fabricated recall result.

State lives in the named Docker volume (SQLite + LanceDB + Kuzu); the container is disposable.
``endpoint.json`` (mode 0600) is the SSOT that lets both the long-lived MCP server and the
short-lived hook subprocesses resolve the same local endpoint; like ``serena/daemon.json`` it is
never trusted for liveness (a fresh ``/health`` probe + ``docker inspect`` decides that).
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import secrets
import shutil
import subprocess  # noqa: S404 - fixed-argv docker CLI calls only, no shell, no user input
import time
from pathlib import Path

import httpx

from repo_agent_harness import paths

LOG = logging.getLogger(__name__)

IN_CONTAINER_PORT = 8000  # cognee's fixed internal HTTP port
_DEFAULT_CONTAINER = "astrojones-cognee"
_DEFAULT_VOLUME = "astrojones-cognee-data"
_DEFAULT_PORT = 8765
_DEFAULT_IMAGE = "cognee/cognee:1.4.0"  # pinned, mirrors astrojones/cognee docker-compose.yml
_DEFAULT_EMAIL = "harness@example.com"  # must be a valid email format (cognee validates it)

# Anti-hallucination summarize-prompt override, vendored from astrojones/cognee's
# prompts/summarize_content.txt (see docker-compose.yml there for the remote's matching mount).
_SUMMARIZE_PROMPT_FILE = Path(__file__).parent / "cognee_local_summarize_prompt.txt"
_SUMMARIZE_PROMPT_CONTAINER_PATH = "/app/cognee/infrastructure/llm/prompts/summarize_content.txt"

# Embedding + LLM config, mirroring the remote deployment so local and remote share one vector
# space. The embedder is the same everywhere on purpose (vectors are model/dimension-specific).
_EMBEDDING_PROVIDER = "openai_compatible"
_EMBEDDING_MODEL = "openai/text-embedding-3-small"
_EMBEDDING_ENDPOINT = "https://openrouter.ai/api/v1"
_EMBEDDING_DIMENSIONS = "1536"
_LLM_PROVIDER = "custom"
_LLM_MODEL = "openai/google/gemini-2.5-flash-lite"
_LLM_ENDPOINT = "https://openrouter.ai/api/v1"
# A non-empty key is required even when COGNEE_SKIP_CONNECTION_TEST suppresses the boot preflight;
# a real OpenRouter key (passed through) makes recall work, the placeholder only keeps boot happy.
_KEY_PLACEHOLDER = "or-local-no-key"  # noqa: S105 - not a secret, a non-empty sentinel

_HEALTH_POLL_INTERVAL_S = 1.0
_DOCKER_CALL_TIMEOUT_S = 30.0
_PULL_TIMEOUT_S = 900.0  # ~1.8 GB image; the CLI `up` surfaces progress separately


# --------------------------------------------------------------------------- config helpers


def container_name() -> str:
    """The singleton container name (override ``COGNEE_LOCAL_CONTAINER``)."""
    return (os.environ.get("COGNEE_LOCAL_CONTAINER") or "").strip() or _DEFAULT_CONTAINER


def volume_name() -> str:
    """The named Docker volume for persistence (override ``COGNEE_LOCAL_VOLUME``)."""
    return (os.environ.get("COGNEE_LOCAL_VOLUME") or "").strip() or _DEFAULT_VOLUME


def port() -> int:
    """The host loopback port bound to the container's 8000 (override ``COGNEE_LOCAL_PORT``)."""
    raw = (os.environ.get("COGNEE_LOCAL_PORT") or "").strip()
    if raw.isdigit():
        return int(raw)
    return _DEFAULT_PORT


def image() -> str:
    """The image tag to run (override ``COGNEE_LOCAL_IMAGE``)."""
    return (os.environ.get("COGNEE_LOCAL_IMAGE") or "").strip() or _DEFAULT_IMAGE


def user_email() -> str:
    """The default-user email seeded on first boot (override ``COGNEE_LOCAL_USER_EMAIL``)."""
    return (os.environ.get("COGNEE_LOCAL_USER_EMAIL") or "").strip() or _DEFAULT_EMAIL


def local_base_url() -> str:
    """The loopback base URL the local container serves."""
    return f"http://127.0.0.1:{port()}"


def openrouter_key() -> str | None:
    """The OpenRouter API key to pass through, searched across the common spellings.

    Never read/printed by the harness for its own sake — it is forwarded verbatim into the
    container's ``LLM_API_KEY``/``EMBEDDING_API_KEY`` and never persisted in ``endpoint.json``.
    """
    spellings = ("COGNEE_LOCAL_LLM_API_KEY", "OPENROUTER_API_KEY", "LLM_API_KEY", "EMBEDDING_API_KEY", "OPENAI_API_KEY")
    for name in spellings:
        val = (os.environ.get(name) or "").strip()
        if val:
            return val
    return None


def enabled() -> bool:
    """Whether the server should AUTO-START local cognee at boot (opt-in).

    ``COGNEE_LOCAL_ENABLE``: ``1`` → arm auto-start (when Docker is available); ``0`` or the
    default ``auto`` → do not auto-start. Auto-start is deliberately opt-in: a machine that
    already runs a remote — even one a GUI-launched server failed to inherit into its env — must
    never silently boot a second, split-brain memory store. The explicit ``cognee-local up`` CLI
    brings local up regardless of this flag; this gate only governs the unprompted boot path.
    """
    raw = (os.environ.get("COGNEE_LOCAL_ENABLE") or "auto").strip().lower()
    if raw in {"1", "true", "yes", "on"}:
        return docker_available()
    return False


# --------------------------------------------------------------------------- docker seam


def _docker(args: list[str], *, timeout: float = _DOCKER_CALL_TIMEOUT_S) -> subprocess.CompletedProcess:
    """Run one ``docker`` CLI invocation, capturing output; never raises on non-zero exit.

    The single seam every container operation flows through, so tests inject a fake by
    monkeypatching this function (no real Docker in CI).
    """
    return subprocess.run(  # noqa: S603 - fixed argv built from our own config, no shell
        ["docker", *args],  # noqa: S607 - docker resolved from PATH by design (blessed CLI)
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def docker_available() -> bool:
    """Whether the ``docker`` CLI is on PATH and its daemon answers (``docker info``)."""
    if shutil.which("docker") is None:
        return False
    try:
        return _docker(["info"], timeout=10.0).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def container_state(name: str) -> str:
    """``running`` / ``exited`` / ``absent`` for ``name`` (via ``docker inspect``)."""
    try:
        proc = _docker(["inspect", "-f", "{{.State.Running}}", name])
    except (OSError, subprocess.SubprocessError):
        return "absent"
    if proc.returncode != 0:
        return "absent"
    return "running" if proc.stdout.strip() == "true" else "exited"


def image_present(tag: str) -> bool:
    """Whether image ``tag`` is already pulled locally."""
    try:
        return _docker(["image", "inspect", tag]).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# --------------------------------------------------------------------------- endpoint.json


def read_endpoint() -> dict | None:
    """The persisted endpoint descriptor, or None when absent/garbage (fail-closed)."""
    try:
        with paths.cognee_endpoint_file().open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write_endpoint(descriptor: dict) -> None:
    """Persist the endpoint descriptor (base_url + creds) at mode 0600, parent dir created."""
    path = paths.cognee_endpoint_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(descriptor), encoding="utf-8")
    tmp.chmod(0o600)
    tmp.replace(path)


def clear_endpoint() -> None:
    """Remove a stale endpoint file (e.g. the container is gone); safe when already absent."""
    with contextlib.suppress(OSError):
        paths.cognee_endpoint_file().unlink()


def _resolve_password() -> str:
    """Reuse the password persisted at first ``up`` (the volume's user), else mint a new one.

    cognee honours ``DEFAULT_USER_PASSWORD`` only at first user creation; later boots reuse the
    persisted user, so the password must survive across restarts — it lives in ``endpoint.json``.
    """
    existing = read_endpoint()
    if existing and isinstance(existing.get("password"), str) and existing["password"]:
        return existing["password"]
    return secrets.token_hex(16)


# --------------------------------------------------------------------------- run recipe


def container_env(email: str, password: str) -> dict[str, str]:
    """The container's environment: persistence paths + the shared OpenRouter embedding/LLM config.

    The embedding config is identical to the remote deployment so vectors are interchangeable.
    The OpenRouter key is passed through when present; absent, a non-empty placeholder keeps boot
    happy (with ``COGNEE_SKIP_CONNECTION_TEST``) while recall stays inert until a key is set.
    """
    key = openrouter_key() or _KEY_PLACEHOLDER
    return {
        "DATA_ROOT_DIRECTORY": "/data/.cognee_data",
        "SYSTEM_ROOT_DIRECTORY": "/data/.cognee_system",
        "DEFAULT_USER_EMAIL": email,
        "DEFAULT_USER_PASSWORD": password,
        "COGNEE_SKIP_CONNECTION_TEST": "true",
        "EMBEDDING_PROVIDER": _EMBEDDING_PROVIDER,
        "EMBEDDING_MODEL": _EMBEDDING_MODEL,
        "EMBEDDING_ENDPOINT": _EMBEDDING_ENDPOINT,
        "EMBEDDING_DIMENSIONS": _EMBEDDING_DIMENSIONS,
        "EMBEDDING_API_KEY": key,
        "LLM_PROVIDER": _LLM_PROVIDER,
        "LLM_MODEL": _LLM_MODEL,
        "LLM_ENDPOINT": _LLM_ENDPOINT,
        "LLM_API_KEY": key,
    }


def docker_run_args(email: str, password: str) -> list[str]:
    """The full ``docker run`` argv (after ``docker``) for a fresh local container.

    Loopback-only publish (never expose memory on the LAN), one named volume for all state,
    ``--restart unless-stopped`` so Docker rewarms it across host reboots. The summarize-prompt
    override is bind-mounted read-only when the vendored copy is present (never a hard
    requirement — an absent file just means the stock in-image prompt is used, same as before
    this override existed).
    """
    args = [
        "run",
        "-d",
        "--name",
        container_name(),
        "--restart",
        "unless-stopped",
        "-p",
        f"127.0.0.1:{port()}:{IN_CONTAINER_PORT}",
        "-v",
        f"{volume_name()}:/data",
    ]
    if _SUMMARIZE_PROMPT_FILE.is_file():
        args += ["-v", f"{_SUMMARIZE_PROMPT_FILE}:{_SUMMARIZE_PROMPT_CONTAINER_PATH}:ro"]
    for key, value in container_env(email, password).items():
        args += ["-e", f"{key}={value}"]
    args.append(image())
    return args


# --------------------------------------------------------------------------- health + auth


def health_ok(base: str, *, timeout: float = 5.0) -> bool:
    """Whether ``GET {base}/health`` returns 200 (cheap liveness, no auth)."""
    try:
        return httpx.get(f"{base}/health", timeout=timeout).status_code == httpx.codes.OK
    except httpx.HTTPError:
        return False


def _poll_health(base: str, budget: float) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if health_ok(base):
            return True
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    return False


def provision_user(base: str, email: str, password: str, *, timeout: float = 30.0) -> bool:
    """Ensure the default user can log in: try login, else register then re-login.

    cognee auto-creates the default superuser lazily; when it hasn't yet, we force it via
    ``/api/v1/auth/register`` so the client's existing bearer-login path succeeds unchanged.
    Returns whether a login ultimately succeeds.
    """
    try:
        with httpx.Client(base_url=base, timeout=timeout) as http:
            if _login_ok(http, email, password):
                return True
            http.post("/api/v1/auth/register", json={"email": email, "password": password})
            return _login_ok(http, email, password)
    except httpx.HTTPError:
        return False


def _login_ok(http: httpx.Client, email: str, password: str) -> bool:
    resp = http.post("/api/v1/auth/login", data={"username": email, "password": password})
    return resp.status_code == httpx.codes.OK


# --------------------------------------------------------------------------- lifecycle


def ensure_local(budget: float = 120.0) -> str | None:
    """Find-or-start the local container and return its base URL once it verifiably answers.

    Idempotent: a running, healthy container is reused (endpoint refreshed, no ``docker run``).
    A stopped one is ``docker start``ed; an absent one is ``docker run``. Cold boot (image import
    + alembic migration) is polled up to ``budget``. On a racing double-start the fixed ``--name``
    collides; the loser falls through to the winner's health-poll — the serena EADDRINUSE shape.
    Returns None (never raises) when Docker is unavailable or the container never becomes healthy.
    """
    if not docker_available():
        return None
    name = container_name()
    base = local_base_url()
    email = user_email()
    password = _resolve_password()

    state = container_state(name)
    if state == "running" and health_ok(base):
        _persist(base, email, password)
        return base
    if state == "absent":
        if not image_present(image()) and not _pull_image():
            return None
        run = _docker(docker_run_args(email, password))
        if run.returncode != 0 and "is already in use" not in (run.stderr or ""):
            LOG.warning("local cognee `docker run` failed: %s", (run.stderr or "").strip()[:300])
            return None
    elif state == "exited":
        _docker(["start", name])

    # Persist the endpoint (with the password we just handed the container) BEFORE the health
    # poll: a fresh cold boot (alembic migrations) can outlast the budget, and if we only wrote
    # on success the generated password would be orphaned — the container's default user would
    # exist with a secret nothing recorded. Written early, a later reuse recovers it. The file is
    # never trusted for liveness (a fresh /health probe decides that), so an early write is safe.
    _persist(base, email, password)
    if not _poll_health(base, budget):
        LOG.warning("local cognee on %s did not become healthy within %.0fs", base, budget)
        return None
    provision_user(base, email, password)
    _persist(base, email, password)
    return base


def _pull_image() -> bool:
    """Pull the configured image; True on success. Long budget — the image is ~1.8 GB."""
    LOG.info("pulling local cognee image %s (first run only)", image())
    return _docker(["pull", image()], timeout=_PULL_TIMEOUT_S).returncode == 0


def _persist(base: str, email: str, password: str) -> None:
    write_endpoint(
        {
            "base_url": base,
            "email": email,
            "password": password,
            "container": container_name(),
            "image": image(),
            "port": port(),
            "started_at": time.time(),
        }
    )


# --------------------------------------------------------------------------- CLI actions


def up(budget: float = _PULL_TIMEOUT_S) -> dict:
    """Bring up (or reuse) the local container, pulling the image if needed. The user's one command."""
    if not docker_available():
        return {"ok": False, "reason": "docker unavailable; install Docker or set COGNEE_BASE_URL"}
    base = ensure_local(budget=budget)
    if base is None:
        return {"ok": False, "reason": "local cognee did not become healthy; see `cognee-local logs`"}
    return {
        "ok": True,
        "endpoint": read_endpoint(),
        "embedding_key_present": openrouter_key() is not None,
    }


def status() -> dict:
    """Report liveness without starting anything."""
    name = container_name()
    state = container_state(name) if docker_available() else "docker-unavailable"
    base = local_base_url()
    return {
        "ok": True,
        "enabled": enabled(),
        "docker_available": docker_available(),
        "container": name,
        "state": state,
        "healthy": state == "running" and health_ok(base),
        "endpoint": read_endpoint(),
        "image": image(),
        "volume": volume_name(),
        "embedding_key_present": openrouter_key() is not None,
    }


def stop() -> dict:
    """Stop the container (data persists in the volume)."""
    proc = _docker(["stop", container_name()])
    return {"ok": proc.returncode == 0, "action": "stop", "container": container_name()}


def down() -> dict:
    """Remove the container, keeping the volume (a fresh ``up`` reattaches the same data)."""
    proc = _docker(["rm", "-f", container_name()])
    clear_endpoint()
    return {"ok": proc.returncode == 0, "action": "down", "container": container_name()}


def logs(tail: int = 80) -> dict:
    """Tail the container logs."""
    proc = _docker(["logs", "--tail", str(tail), container_name()])
    return {"ok": proc.returncode == 0, "logs": (proc.stdout or "") + (proc.stderr or "")}


def nuke() -> dict:
    """Destroy the container AND its volume — the only irreversible action (confirm-gated by CLI)."""
    _docker(["rm", "-f", container_name()])
    vol = _docker(["volume", "rm", volume_name()])
    clear_endpoint()
    return {"ok": vol.returncode == 0, "action": "nuke", "container": container_name(), "volume": volume_name()}
