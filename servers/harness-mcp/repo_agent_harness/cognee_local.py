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

Storage backend (``COGNEE_LOCAL_BACKEND``): the default ``postgres`` adds a pgvector/pgvector:pg17
sidecar on a shared bridge network, serving relational + vector + graph from one ``cognee_db`` —
byte-for-byte the remote's storage layer, so local recall exercises the same engines (true parity).
``embedded`` is the old sidecar-free path (SQLite + LanceDB + Kuzu in the cognee volume): lighter,
but not storage-representative of the remote. Either way the compute layer (image, LLM, embedder,
summarize prompt) is identical.

State lives in the named Docker volume(s); the container(s) are disposable.
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

# Storage backend. ``postgres`` (default) mirrors the remote deployment exactly — one
# pgvector/pgvector:pg17 serving relational + vector + graph in a single ``cognee_db`` — so
# local recall exercises the same pgvector/PG-graph engines as ``cognee.bartix.de`` (true
# parity, at the cost of a second container). ``embedded`` is the old zero-sidecar path
# (SQLite + LanceDB + Kuzu in the cognee volume): lighter, but NOT storage-representative of
# the remote. The compute layer (image, LLM, embedder, summarize prompt) is identical either way.
_DEFAULT_BACKEND = "postgres"
_PG_IMAGE = "pgvector/pgvector:pg17"  # mirrors astrojones/cognee docker-compose.yml
_PG_CONTAINER = "astrojones-cognee-postgres"
_PG_VOLUME = "astrojones-cognee-pgdata"
_PG_NETWORK = "astrojones-cognee-net"  # user-defined bridge (host-net is unreliable on macOS)
_PG_USER = "cognee"
_PG_PASSWORD = "cognee"  # noqa: S105 - local dev DB on a loopback-only bridge, mirrors remote compose
_PG_DB = "cognee_db"
_PG_PORT = 5432  # in-container; not published to the host by default (containers talk over the bridge)
# Mirror the remote's PG tuning so the pool-behaviour is representative (the remote raised these
# after cognify/improve bursts exhausted the default 100 connections — see docker-compose.yml).
_PG_MAX_CONNECTIONS = "300"
_PG_SHARED_BUFFERS = "1GB"

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


def backend() -> str:
    """The storage backend: ``postgres`` (default, remote-parity) or ``embedded``.

    Override with ``COGNEE_LOCAL_BACKEND``; any value other than ``embedded`` resolves to
    ``postgres`` so a typo fails safe toward the parity path rather than the divergent one.
    """
    raw = (os.environ.get("COGNEE_LOCAL_BACKEND") or "").strip().lower()
    return "embedded" if raw == "embedded" else _DEFAULT_BACKEND


def uses_postgres() -> bool:
    """Whether the postgres/pgvector sidecar is part of the stack."""
    return backend() == "postgres"


def pg_container_name() -> str:
    """The postgres sidecar container name (override ``COGNEE_LOCAL_PG_CONTAINER``)."""
    return (os.environ.get("COGNEE_LOCAL_PG_CONTAINER") or "").strip() or _PG_CONTAINER


def pg_volume_name() -> str:
    """The postgres data volume (override ``COGNEE_LOCAL_PG_VOLUME``)."""
    return (os.environ.get("COGNEE_LOCAL_PG_VOLUME") or "").strip() or _PG_VOLUME


def pg_image() -> str:
    """The postgres image tag (override ``COGNEE_LOCAL_PG_IMAGE``)."""
    return (os.environ.get("COGNEE_LOCAL_PG_IMAGE") or "").strip() or _PG_IMAGE


def network_name() -> str:
    """The user-defined bridge both containers join (override ``COGNEE_LOCAL_NETWORK``)."""
    return (os.environ.get("COGNEE_LOCAL_NETWORK") or "").strip() or _PG_NETWORK


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


# --------------------------------------------------------------------------- postgres sidecar


def ensure_network() -> bool:
    """Idempotently create the shared bridge network; True when it exists afterwards."""
    if _docker(["network", "inspect", network_name()]).returncode == 0:
        return True
    created = _docker(["network", "create", network_name()])
    # A racing peer may have created it between our inspect and create — treat "already exists" as ok.
    return created.returncode == 0 or "already exists" in (created.stderr or "")


def pg_run_args() -> list[str]:
    """The full ``docker run`` argv (after ``docker``) for the pgvector sidecar.

    One named volume for ``/var/lib/postgresql/data``; joined to the shared bridge under a fixed
    alias so cognee reaches it by name. ``max_connections``/``shared_buffers`` mirror the remote so
    the connection-pool behaviour is representative. Not published to the host by default (the
    bridge is enough for cognee); set ``COGNEE_LOCAL_PG_PORT`` to also expose it on loopback for psql.
    """
    args = [
        "run",
        "-d",
        "--name",
        pg_container_name(),
        "--restart",
        "unless-stopped",
        "--network",
        network_name(),
        "-v",
        f"{pg_volume_name()}:/var/lib/postgresql/data",
        "-e",
        f"POSTGRES_USER={_PG_USER}",
        "-e",
        f"POSTGRES_PASSWORD={_PG_PASSWORD}",
        "-e",
        f"POSTGRES_DB={_PG_DB}",
    ]
    host_port = (os.environ.get("COGNEE_LOCAL_PG_PORT") or "").strip()
    if host_port.isdigit():
        args += ["-p", f"127.0.0.1:{host_port}:{_PG_PORT}"]
    args += [
        pg_image(),
        "postgres",
        "-c",
        f"max_connections={_PG_MAX_CONNECTIONS}",
        "-c",
        f"shared_buffers={_PG_SHARED_BUFFERS}",
    ]
    return args


def pg_ready(name: str, *, timeout: float = _DOCKER_CALL_TIMEOUT_S) -> bool:
    """Whether postgres accepts connections (``pg_isready`` inside the container)."""
    proc = _docker(["exec", name, "pg_isready", "-U", _PG_USER, "-d", _PG_DB], timeout=timeout)
    return proc.returncode == 0


def _poll_pg(name: str, budget: float) -> bool:
    deadline = time.monotonic() + budget
    while time.monotonic() < deadline:
        if pg_ready(name):
            return True
        time.sleep(_HEALTH_POLL_INTERVAL_S)
    return False


def ensure_postgres(budget: float = 60.0) -> bool:
    """Find-or-start the pgvector sidecar and return once it accepts connections.

    Idempotent, mirroring ``ensure_local``: a running+ready container is reused, a stopped one is
    started, an absent one is run (pulling the image first). Returns False (never raises) when the
    network can't be created, the image can't be pulled, the run fails, or PG never becomes ready
    within ``budget`` — the caller then aborts the cognee boot rather than let it hit a dead DB.
    """
    if not ensure_network():
        LOG.warning("could not create local cognee network %s", network_name())
        return False
    name = pg_container_name()
    state = container_state(name)
    if state == "running" and pg_ready(name):
        return True
    if state == "absent":
        if not image_present(pg_image()) and _docker(["pull", pg_image()], timeout=_PULL_TIMEOUT_S).returncode != 0:
            LOG.warning("could not pull postgres image %s", pg_image())
            return False
        run = _docker(pg_run_args())
        if run.returncode != 0 and "is already in use" not in (run.stderr or ""):
            LOG.warning("local cognee postgres `docker run` failed: %s", (run.stderr or "").strip()[:300])
            return False
    elif state == "exited":
        _docker(["start", name])
    return _poll_pg(name, budget)


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
    env = {
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
    if uses_postgres():
        env.update(pg_client_env())
    return env


def pg_client_env() -> dict[str, str]:
    """Cognee's DB/vector/graph connection env pointing at the pgvector sidecar.

    Mirrors the remote deployment: all three stores share one ``cognee_db`` on one postgres,
    reached by the sidecar's network alias (``pg_container_name``) over the shared bridge — the
    same value cognee's alembic migration and pgvector writes both use.
    """
    host = pg_container_name()
    common = {"HOST": host, "PORT": str(_PG_PORT), "USERNAME": _PG_USER, "PASSWORD": _PG_PASSWORD, "NAME": _PG_DB}
    return {
        "DB_PROVIDER": "postgres",
        "VECTOR_DB_PROVIDER": "pgvector",
        "GRAPH_DATABASE_PROVIDER": "postgres",
        **{f"DB_{k}": v for k, v in common.items()},
        **{f"VECTOR_DB_{k}": v for k, v in common.items()},
        **{f"GRAPH_DATABASE_{k}": v for k, v in common.items()},
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
    if uses_postgres():  # join the shared bridge so cognee reaches the pgvector sidecar by name
        args += ["--network", network_name()]
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


def ensure_local(budget: float = 120.0) -> str | None:  # noqa: PLR0911 - legitimate early-exit guards (docker/pg/state/health branches)
    """Find-or-start the local container and return its base URL once it verifiably answers.

    Idempotent: a running, healthy container is reused (endpoint refreshed, no ``docker run``).
    A stopped one is ``docker start``ed; an absent one is ``docker run``. Cold boot (image import
    + alembic migration) is polled up to ``budget``. On a racing double-start the fixed ``--name``
    collides; the loser falls through to the winner's health-poll — the serena EADDRINUSE shape.
    Returns None (never raises) when Docker is unavailable or the container never becomes healthy.
    """
    if not docker_available():
        return None
    if uses_postgres() and not ensure_postgres():
        LOG.warning("local cognee postgres sidecar did not come up; aborting cognee boot")
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
    have_docker = docker_available()
    name = container_name()
    state = container_state(name) if have_docker else "docker-unavailable"
    base = local_base_url()
    out = {
        "ok": True,
        "enabled": enabled(),
        "docker_available": have_docker,
        "backend": backend(),
        "container": name,
        "state": state,
        "healthy": state == "running" and health_ok(base),
        "endpoint": read_endpoint(),
        "image": image(),
        "volume": volume_name(),
        "embedding_key_present": openrouter_key() is not None,
    }
    if uses_postgres():
        pg = pg_container_name()
        pg_state = container_state(pg) if have_docker else "docker-unavailable"
        out["postgres"] = {
            "container": pg,
            "state": pg_state,
            "ready": have_docker and pg_state == "running" and pg_ready(pg),
            "image": pg_image(),
            "volume": pg_volume_name(),
            "network": network_name(),
        }
    return out


def stop() -> dict:
    """Stop the container(s) (data persists in the volume(s))."""
    proc = _docker(["stop", container_name()])
    ok = proc.returncode == 0
    if uses_postgres():
        ok = _docker(["stop", pg_container_name()]).returncode == 0 and ok
    return {"ok": ok, "action": "stop", "container": container_name()}


def down() -> dict:
    """Remove the container(s), keeping the volume(s) (a fresh ``up`` reattaches the same data)."""
    proc = _docker(["rm", "-f", container_name()])
    ok = proc.returncode == 0
    if uses_postgres():
        ok = _docker(["rm", "-f", pg_container_name()]).returncode == 0 and ok
        _docker(["network", "rm", network_name()])  # best-effort; harmless if absent or still referenced
    clear_endpoint()
    return {"ok": ok, "action": "down", "container": container_name()}


def logs(tail: int = 80) -> dict:
    """Tail the container logs."""
    proc = _docker(["logs", "--tail", str(tail), container_name()])
    return {"ok": proc.returncode == 0, "logs": (proc.stdout or "") + (proc.stderr or "")}


def nuke() -> dict:
    """Destroy the container(s) AND volume(s) — the only irreversible action (confirm-gated by CLI).

    In postgres mode this also removes the pgvector sidecar, its data volume, and the shared
    network, so a subsequent ``up`` starts from a genuinely fresh database (the parity test's
    precondition). The cognee volume's removal result is the reported ``ok``.
    """
    _docker(["rm", "-f", container_name()])
    vol = _docker(["volume", "rm", volume_name()])
    result = {"ok": vol.returncode == 0, "action": "nuke", "container": container_name(), "volume": volume_name()}
    if uses_postgres():
        _docker(["rm", "-f", pg_container_name()])
        pg_vol = _docker(["volume", "rm", pg_volume_name()])
        _docker(["network", "rm", network_name()])
        result["ok"] = result["ok"] and pg_vol.returncode == 0
        result["postgres_volume"] = pg_volume_name()
    clear_endpoint()
    return result
