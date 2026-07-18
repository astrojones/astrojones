"""Unit tests for the local-cognee lifecycle: docker CLI + health/auth behind seams (no real Docker).

Every container operation flows through ``cognee_local._docker`` and the ``health_ok`` /
``provision_user`` / ``image_present`` helpers, so tests drive the whole lifecycle by
monkeypatching those seams and pointing ``REPO_AGENT_HARNESS_HOME`` at a tmp dir for
``endpoint.json``. Mirrors the fake-runner discipline of ``test_docker_scripts.py``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from repo_agent_harness import cognee_local


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    """Point harness home at a tmp dir and clear every COGNEE_* knob for a clean baseline."""
    monkeypatch.setenv("REPO_AGENT_HARNESS_HOME", str(tmp_path))
    for var in (
        "COGNEE_BASE_URL",
        "COGNEE_LOCAL_ENABLE",
        "COGNEE_LOCAL_PORT",
        "COGNEE_LOCAL_IMAGE",
        "COGNEE_LOCAL_CONTAINER",
        "COGNEE_LOCAL_VOLUME",
        "COGNEE_LOCAL_USER_EMAIL",
        "COGNEE_LOCAL_LLM_API_KEY",
        "COGNEE_LOCAL_BACKEND",
        "COGNEE_LOCAL_PG_CONTAINER",
        "COGNEE_LOCAL_PG_VOLUME",
        "COGNEE_LOCAL_PG_IMAGE",
        "COGNEE_LOCAL_PG_PORT",
        "COGNEE_LOCAL_NETWORK",
        "OPENROUTER_API_KEY",
        "LLM_API_KEY",
        "EMBEDDING_API_KEY",
        "OPENAI_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    return tmp_path


def _completed(returncode=0, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr)


# --------------------------------------------------------------------------- enabled()


def test_enabled_default_auto_does_not_autostart(monkeypatch):
    # Default (auto): opt-in, so the server never auto-boots even with Docker present.
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    assert cognee_local.enabled() is False


def test_enabled_off_never(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_ENABLE", "0")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    assert cognee_local.enabled() is False


def test_enabled_armed_requires_docker(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_ENABLE", "1")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    assert cognee_local.enabled() is True  # armed + docker → auto-start
    monkeypatch.setattr(cognee_local, "docker_available", lambda: False)
    assert cognee_local.enabled() is False  # armed but no docker → can't boot


# --------------------------------------------------------------------------- run recipe


def test_docker_run_args_recipe():
    args = cognee_local.docker_run_args("me@example.com", "pw123")
    assert args[:2] == ["run", "-d"]
    assert "--name" in args and cognee_local.container_name() in args
    assert "127.0.0.1:8765:8000" in args  # loopback-only publish
    assert f"{cognee_local.volume_name()}:/data" in args
    assert args[-1] == cognee_local.image()
    assert "--restart" in args and "unless-stopped" in args


def test_docker_run_args_mounts_summarize_prompt_when_vendored():
    assert cognee_local._SUMMARIZE_PROMPT_FILE.is_file()  # vendored from astrojones/cognee
    args = cognee_local.docker_run_args("me@example.com", "pw123")
    mount = f"{cognee_local._SUMMARIZE_PROMPT_FILE}:{cognee_local._SUMMARIZE_PROMPT_CONTAINER_PATH}:ro"
    assert mount in args


def test_docker_run_args_skips_prompt_mount_when_file_absent(monkeypatch):
    monkeypatch.setattr(cognee_local, "_SUMMARIZE_PROMPT_FILE", Path("/nonexistent/prompt.txt"))
    args = cognee_local.docker_run_args("me@example.com", "pw123")
    assert not any("summarize" in a for a in args)


def test_container_env_mirrors_remote_embedding():
    env = cognee_local.container_env("me@example.com", "pw123")
    assert env["EMBEDDING_MODEL"] == "openai/text-embedding-3-small"
    assert env["EMBEDDING_ENDPOINT"] == "https://openrouter.ai/api/v1"
    assert env["EMBEDDING_DIMENSIONS"] == "1536"
    assert env["EMBEDDING_PROVIDER"] == "openai_compatible"
    assert env["LLM_ENDPOINT"] == "https://openrouter.ai/api/v1"
    assert env["DEFAULT_USER_EMAIL"] == "me@example.com"
    assert env["COGNEE_SKIP_CONNECTION_TEST"] == "true"


def test_container_env_passes_key_through(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    env = cognee_local.container_env("me@example.com", "pw")
    assert env["EMBEDDING_API_KEY"] == "sk-or-secret"
    assert env["LLM_API_KEY"] == "sk-or-secret"


def test_container_env_placeholder_without_key():
    env = cognee_local.container_env("me@example.com", "pw")
    assert env["EMBEDDING_API_KEY"] == cognee_local._KEY_PLACEHOLDER
    assert env["LLM_API_KEY"] == cognee_local._KEY_PLACEHOLDER


def test_openrouter_key_priority(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")
    monkeypatch.setenv("COGNEE_LOCAL_LLM_API_KEY", "sk-explicit")
    assert cognee_local.openrouter_key() == "sk-explicit"


# --------------------------------------------------------------------------- endpoint.json


def test_endpoint_roundtrip_mode_0600():
    cognee_local.write_endpoint({"base_url": "http://127.0.0.1:8765", "password": "abc"})
    assert cognee_local.read_endpoint()["password"] == "abc"
    mode = cognee_local.write_endpoint  # noqa: F841 - keep the call above readable
    assert (cognee_local.paths.cognee_endpoint_file().stat().st_mode & 0o777) == 0o600


def test_read_endpoint_missing_is_none():
    assert cognee_local.read_endpoint() is None


def test_resolve_password_reuses_persisted():
    cognee_local.write_endpoint({"base_url": "x", "password": "persisted-pw"})
    assert cognee_local._resolve_password() == "persisted-pw"


def test_resolve_password_mints_when_absent():
    pw = cognee_local._resolve_password()
    assert len(pw) == 32  # secrets.token_hex(16)


# --------------------------------------------------------------------------- container_state


@pytest.mark.parametrize(
    ("rc", "stdout", "expected"),
    [(0, "true\n", "running"), (0, "false\n", "exited"), (1, "", "absent")],
)
def test_container_state_parsing(monkeypatch, rc, stdout, expected):
    monkeypatch.setattr(cognee_local, "_docker", lambda *a, **k: _completed(rc, stdout))
    assert cognee_local.container_state("astrojones-cognee") == expected


# --------------------------------------------------------------------------- ensure_local


def test_ensure_local_none_without_docker(monkeypatch):
    monkeypatch.setattr(cognee_local, "docker_available", lambda: False)
    assert cognee_local.ensure_local() is None


def test_ensure_local_reuses_running_healthy(monkeypatch):
    calls = []
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")  # cognee-container lifecycle in isolation
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "running")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda *a, **k: calls.append(a) or _completed())
    base = cognee_local.ensure_local()
    assert base == "http://127.0.0.1:8765"
    assert calls == []  # reuse path issues no docker run/start
    assert cognee_local.read_endpoint()["base_url"] == base


def test_ensure_local_cold_run_then_healthy(monkeypatch):
    ran = []
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "absent")
    monkeypatch.setattr(cognee_local, "image_present", lambda tag: True)
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "provision_user", lambda *a, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: ran.append(args) or _completed())
    base = cognee_local.ensure_local()
    assert base == "http://127.0.0.1:8765"
    assert ran and ran[0][0] == "run"  # a fresh container was launched
    assert cognee_local.read_endpoint()["container"] == "astrojones-cognee"


def test_ensure_local_pulls_missing_image(monkeypatch):
    ran = []
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "absent")
    monkeypatch.setattr(cognee_local, "image_present", lambda tag: False)
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "provision_user", lambda *a, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: ran.append(args[0]) or _completed())
    cognee_local.ensure_local()
    assert "pull" in ran and "run" in ran
    assert ran.index("pull") < ran.index("run")  # pull precedes run


def test_ensure_local_race_name_in_use_still_polls(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "absent")
    monkeypatch.setattr(cognee_local, "image_present", lambda tag: True)
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "provision_user", lambda *a, **k: True)
    monkeypatch.setattr(
        cognee_local,
        "_docker",
        lambda args, **k: _completed(1, stderr='Conflict. The container name "/x" is already in use'),
    )
    assert cognee_local.ensure_local() == "http://127.0.0.1:8765"  # loser falls through to health-poll


def test_ensure_local_run_failure_returns_none(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "absent")
    monkeypatch.setattr(cognee_local, "image_present", lambda tag: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: _completed(1, stderr="no space left on device"))
    assert cognee_local.ensure_local() is None


def test_ensure_local_persists_endpoint_before_health_poll(monkeypatch):
    """A cold boot that outlasts the budget still records the password — never orphan the login.

    The container is launched (default user created with a generated password) but health never
    comes up within budget=0. ensure_local returns None, yet endpoint.json must already carry the
    password so a later reuse can log in — otherwise the volume's user is unreachable forever.
    """
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "absent")
    monkeypatch.setattr(cognee_local, "image_present", lambda tag: True)
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: False)
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: _completed())
    assert cognee_local.ensure_local(budget=0) is None  # never became healthy
    persisted = cognee_local.read_endpoint()
    assert persisted is not None and persisted["password"]  # password recorded despite the timeout


def test_ensure_local_starts_exited_container(monkeypatch):
    ran = []
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "exited")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "provision_user", lambda *a, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: ran.append(args[0]) or _completed())
    cognee_local.ensure_local()
    assert ran == ["start"]  # a stopped container is started, not re-run


# --------------------------------------------------------------------------- CLI actions


def test_status_reports_state(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "running")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    out = cognee_local.status()
    assert out["state"] == "running"
    assert out["healthy"] is True
    assert out["image"] == cognee_local.image()


def test_stop_down_nuke_issue_docker_verbs_embedded(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    seen = []
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: seen.append(args[0]) or _completed())
    cognee_local.stop()
    cognee_local.down()
    cognee_local.nuke()
    assert seen == ["stop", "rm", "rm", "volume"]  # single container, single volume


def test_stop_down_nuke_issue_docker_verbs_postgres(monkeypatch):
    # Default backend (postgres): each verb also acts on the pg sidecar + shared network.
    seen = []
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: seen.append(tuple(args)) or _completed())
    cognee_local.stop()
    cognee_local.down()
    cognee_local.nuke()
    pg = cognee_local.pg_container_name()
    net = cognee_local.network_name()
    assert ["stop", pg] in [list(a) for a in seen]  # sidecar stopped alongside cognee
    assert ["rm", "-f", pg] in [list(a) for a in seen]  # sidecar removed on down/nuke
    assert ["volume", "rm", cognee_local.pg_volume_name()] in [list(a) for a in seen]  # pg data destroyed on nuke
    assert ["network", "rm", net] in [list(a) for a in seen]  # shared bridge torn down


def test_down_clears_endpoint(monkeypatch):
    cognee_local.write_endpoint({"base_url": "x"})
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: _completed())
    cognee_local.down()
    assert cognee_local.read_endpoint() is None


# --------------------------------------------------------------------------- pgvector backend


def test_backend_default_is_postgres():
    assert cognee_local.backend() == "postgres"
    assert cognee_local.uses_postgres() is True


def test_backend_embedded_opt_out(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    assert cognee_local.backend() == "embedded"
    assert cognee_local.uses_postgres() is False


def test_backend_typo_fails_safe_to_postgres(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "sqlite")  # anything != embedded → parity path
    assert cognee_local.backend() == "postgres"


def test_container_env_has_pg_wiring_under_postgres():
    env = cognee_local.container_env("me@example.com", "pw")
    assert env["DB_PROVIDER"] == "postgres"
    assert env["VECTOR_DB_PROVIDER"] == "pgvector"
    assert env["GRAPH_DATABASE_PROVIDER"] == "postgres"
    # all three stores point at the one sidecar / one db, by network alias
    for prefix in ("DB", "VECTOR_DB", "GRAPH_DATABASE"):
        assert env[f"{prefix}_HOST"] == cognee_local.pg_container_name()
        assert env[f"{prefix}_NAME"] == "cognee_db"
        assert env[f"{prefix}_PORT"] == "5432"


def test_container_env_no_pg_wiring_under_embedded(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    env = cognee_local.container_env("me@example.com", "pw")
    assert "DB_PROVIDER" not in env
    assert "VECTOR_DB_PROVIDER" not in env


def test_docker_run_args_joins_network_under_postgres():
    args = cognee_local.docker_run_args("me@example.com", "pw")
    assert "--network" in args
    assert cognee_local.network_name() in args
    assert args[-1] == cognee_local.image()  # image still last


def test_docker_run_args_no_network_under_embedded(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    args = cognee_local.docker_run_args("me@example.com", "pw")
    assert "--network" not in args


def test_pg_run_args_recipe():
    args = cognee_local.pg_run_args()
    assert args[:2] == ["run", "-d"]
    assert "--name" in args and cognee_local.pg_container_name() in args
    assert "--network" in args and cognee_local.network_name() in args
    assert f"{cognee_local.pg_volume_name()}:/var/lib/postgresql/data" in args
    assert "POSTGRES_DB=cognee_db" in args
    assert "max_connections=300" in args and "shared_buffers=1GB" in args
    assert not any(a.startswith("127.0.0.1:") for a in args)  # no host publish by default


def test_pg_run_args_publishes_when_port_set(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_PG_PORT", "5433")
    args = cognee_local.pg_run_args()
    assert "-p" in args and "127.0.0.1:5433:5432" in args


def test_ensure_network_reuses_existing(monkeypatch):
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: _completed(0))  # inspect ok
    assert cognee_local.ensure_network() is True


def test_ensure_network_creates_when_absent(monkeypatch):
    calls = []

    def fake(args, **k):
        calls.append(args[:2])
        return _completed(1) if args[:2] == ["network", "inspect"] else _completed(0)

    monkeypatch.setattr(cognee_local, "_docker", fake)
    assert cognee_local.ensure_network() is True
    assert ["network", "create"] in calls


def test_ensure_postgres_reuses_running_ready(monkeypatch):
    monkeypatch.setattr(cognee_local, "ensure_network", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "running")
    monkeypatch.setattr(cognee_local, "pg_ready", lambda name, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda *a, **k: pytest.fail("should not run docker on reuse"))
    assert cognee_local.ensure_postgres() is True


def test_ensure_postgres_cold_run_then_ready(monkeypatch):
    ran = []
    monkeypatch.setattr(cognee_local, "ensure_network", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "absent")
    monkeypatch.setattr(cognee_local, "image_present", lambda tag: True)
    monkeypatch.setattr(cognee_local, "pg_ready", lambda name, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: ran.append(args[0]) or _completed())
    assert cognee_local.ensure_postgres() is True
    assert ran and ran[0] == "run"


def test_ensure_postgres_false_when_network_fails(monkeypatch):
    monkeypatch.setattr(cognee_local, "ensure_network", lambda: False)
    assert cognee_local.ensure_postgres() is False


def test_ensure_local_aborts_when_postgres_down(monkeypatch):
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "ensure_postgres", lambda *a, **k: False)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: pytest.fail("must not reach cognee boot"))
    assert cognee_local.ensure_local() is None


def test_ensure_local_ensures_postgres_first(monkeypatch):
    order = []
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "ensure_postgres", lambda *a, **k: order.append("pg") or True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: order.append("cognee") or "running")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda *a, **k: _completed())
    cognee_local.ensure_local()
    assert order[0] == "pg"  # sidecar readiness gates the cognee boot


def test_status_reports_postgres_block(monkeypatch):
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "running")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "pg_ready", lambda name, **k: True)
    out = cognee_local.status()
    assert out["backend"] == "postgres"
    assert out["postgres"]["container"] == cognee_local.pg_container_name()
    assert out["postgres"]["ready"] is True


def test_status_no_postgres_block_under_embedded(monkeypatch):
    monkeypatch.setenv("COGNEE_LOCAL_BACKEND", "embedded")
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "running")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    out = cognee_local.status()
    assert out["backend"] == "embedded"
    assert "postgres" not in out
