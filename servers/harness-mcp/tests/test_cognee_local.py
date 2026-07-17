"""Unit tests for the local-cognee lifecycle: docker CLI + health/auth behind seams (no real Docker).

Every container operation flows through ``cognee_local._docker`` and the ``health_ok`` /
``provision_user`` / ``image_present`` helpers, so tests drive the whole lifecycle by
monkeypatching those seams and pointing ``REPO_AGENT_HARNESS_HOME`` at a tmp dir for
``endpoint.json``. Mirrors the fake-runner discipline of ``test_docker_scripts.py``.
"""

from __future__ import annotations

import subprocess

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
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "exited")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    monkeypatch.setattr(cognee_local, "provision_user", lambda *a, **k: True)
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: ran.append(args[0]) or _completed())
    cognee_local.ensure_local()
    assert ran == ["start"]  # a stopped container is started, not re-run


# --------------------------------------------------------------------------- CLI actions


def test_status_reports_state(monkeypatch):
    monkeypatch.setattr(cognee_local, "docker_available", lambda: True)
    monkeypatch.setattr(cognee_local, "container_state", lambda name: "running")
    monkeypatch.setattr(cognee_local, "health_ok", lambda base, **k: True)
    out = cognee_local.status()
    assert out["state"] == "running"
    assert out["healthy"] is True
    assert out["image"] == cognee_local.image()


def test_stop_down_nuke_issue_docker_verbs(monkeypatch):
    seen = []
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: seen.append(args[0]) or _completed())
    cognee_local.stop()
    cognee_local.down()
    cognee_local.nuke()
    assert seen == ["stop", "rm", "rm", "volume"]


def test_down_clears_endpoint(monkeypatch):
    cognee_local.write_endpoint({"base_url": "x"})
    monkeypatch.setattr(cognee_local, "_docker", lambda args, **k: _completed())
    cognee_local.down()
    assert cognee_local.read_endpoint() is None
