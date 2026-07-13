"""Digest provider abstraction: config resolution, and each backend in isolation."""

import json
import stat

import httpx
import pytest
from repo_agent_harness import digest_providers as dp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------- config


def test_selected_provider_name_defaults_to_openrouter(monkeypatch):
    monkeypatch.delenv(dp.PROVIDER_ENV, raising=False)
    assert dp.selected_provider_name() == "openrouter"


@pytest.mark.parametrize("off_value", ["off", "OFF", "0", "false", ""])
def test_selected_provider_name_off_switch(monkeypatch, off_value):
    monkeypatch.setenv(dp.PROVIDER_ENV, off_value)
    assert dp.selected_provider_name() is None


def test_selected_provider_name_switches_backend(monkeypatch):
    monkeypatch.setenv(dp.PROVIDER_ENV, "ollama")
    assert dp.selected_provider_name() == "ollama"


def test_selected_model_falls_back_to_provider_default(monkeypatch):
    monkeypatch.delenv(dp.MODEL_ENV, raising=False)
    assert dp.selected_model("openrouter") == dp.MODEL_DEFAULTS["openrouter"]
    assert dp.selected_model("ollama") == dp.MODEL_DEFAULTS["ollama"]


def test_selected_model_env_override_wins(monkeypatch):
    monkeypatch.setenv(dp.MODEL_ENV, "some/other-model")
    assert dp.selected_model("openrouter") == "some/other-model"


def test_build_provider_unknown_name_is_none():
    assert dp.build_provider("not-a-real-provider") is None


def test_build_provider_claude_needs_no_credentials():
    assert isinstance(dp.build_provider("claude"), dp.ClaudeSubscriptionProvider)


def test_build_provider_ollama_reads_base_url_env(monkeypatch):
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://example.internal:11434")
    provider = dp.build_provider("ollama")
    assert isinstance(provider, dp.OllamaProvider)
    assert provider.base_url == "http://example.internal:11434"


def test_build_provider_openrouter_needs_a_key(monkeypatch, tmp_path):
    monkeypatch.delenv(dp._OPENROUTER_KEY_ENV, raising=False)
    monkeypatch.setattr(dp, "_CCR_CONFIG_PATH", tmp_path / "does-not-exist.json")
    assert dp.build_provider("openrouter") is None


def test_openrouter_key_prefers_env_over_ccr_config(monkeypatch, tmp_path):
    ccr_config = tmp_path / "config.json"
    ccr_config.write_text(json.dumps({"Providers": [{"name": "openrouter", "api_key": "ccr-key"}]}))
    monkeypatch.setattr(dp, "_CCR_CONFIG_PATH", ccr_config)
    monkeypatch.setenv(dp._OPENROUTER_KEY_ENV, "env-key")
    assert dp._openrouter_api_key() == "env-key"


def test_openrouter_key_falls_back_to_ccr_config(monkeypatch, tmp_path):
    monkeypatch.delenv(dp._OPENROUTER_KEY_ENV, raising=False)
    ccr_config = tmp_path / "config.json"
    ccr_config.write_text(json.dumps({"Providers": [{"name": "openrouter", "api_key": "ccr-key"}]}))
    monkeypatch.setattr(dp, "_CCR_CONFIG_PATH", ccr_config)
    assert dp._openrouter_api_key() == "ccr-key"


# ----------------------------------------------------------------------------- digest()


async def test_digest_off_ships_raw(monkeypatch):
    monkeypatch.setenv(dp.PROVIDER_ENV, "off")
    assert await dp.digest(["entry"]) is None


async def test_digest_empty_entries_is_noop(monkeypatch):
    monkeypatch.delenv(dp.PROVIDER_ENV, raising=False)
    assert await dp.digest([]) is None


async def test_digest_falls_back_when_provider_unbuildable(monkeypatch, tmp_path):
    """Openrouter selected but no key anywhere: fall back to raw, no crash."""
    monkeypatch.delenv(dp.PROVIDER_ENV, raising=False)
    monkeypatch.delenv(dp._OPENROUTER_KEY_ENV, raising=False)
    monkeypatch.setattr(dp, "_CCR_CONFIG_PATH", tmp_path / "does-not-exist.json")
    assert await dp.digest(["entry"]) is None


async def test_digest_wires_entries_into_prompt_and_calls_provider(monkeypatch):
    seen = {}

    class _FakeProvider:
        async def digest(self, prompt, model):
            seen["prompt"] = prompt
            seen["model"] = model
            return "digested"

    monkeypatch.setenv(dp.PROVIDER_ENV, "openrouter")
    monkeypatch.setattr(dp, "build_provider", lambda name: _FakeProvider())
    assert await dp.digest(["one", "two"]) == "digested"
    assert "one" in seen["prompt"]
    assert "two" in seen["prompt"]
    assert seen["model"] == dp.MODEL_DEFAULTS["openrouter"]


# ------------------------------------------------------------------------ OpenRouterProvider


async def test_openrouter_provider_success():
    def _handle(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["model"] == "google/gemini-3.1-flash-lite"
        assert body["messages"][0]["content"] == "prompt text"
        return httpx.Response(200, json={"choices": [{"message": {"content": "  digested  "}}]})

    provider = dp.OpenRouterProvider(api_key="test-key", transport=httpx.MockTransport(_handle))
    assert await provider.digest("prompt text", "google/gemini-3.1-flash-lite") == "digested"


async def test_openrouter_provider_non_200_returns_none():
    provider = dp.OpenRouterProvider(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert await provider.digest("p", "m") is None


async def test_openrouter_provider_network_error_returns_none():
    def _raise(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg, request=request)

    provider = dp.OpenRouterProvider(api_key="k", transport=httpx.MockTransport(_raise))
    assert await provider.digest("p", "m") is None


async def test_openrouter_provider_malformed_body_returns_none():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"unexpected": "shape"}))
    provider = dp.OpenRouterProvider(api_key="k", transport=transport)
    assert await provider.digest("p", "m") is None


async def test_openrouter_provider_empty_text_returns_none():
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]}))
    provider = dp.OpenRouterProvider(api_key="k", transport=transport)
    assert await provider.digest("p", "m") is None


# --------------------------------------------------------------------------- OllamaProvider


async def test_ollama_provider_success():
    def _handle(request: httpx.Request) -> httpx.Response:
        assert request.url == httpx.URL("http://localhost:11434/api/chat")
        body = json.loads(request.content)
        assert body["model"] == "gemini-3-flash-preview:cloud"
        assert body["stream"] is False
        return httpx.Response(200, json={"message": {"content": "digested"}})

    provider = dp.OllamaProvider(transport=httpx.MockTransport(_handle))
    assert await provider.digest("prompt text", "gemini-3-flash-preview:cloud") == "digested"


async def test_ollama_provider_custom_base_url():
    def _handle(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://example.internal:11434/api/chat"
        return httpx.Response(200, json={"message": {"content": "digested"}})

    provider = dp.OllamaProvider(base_url="http://example.internal:11434", transport=httpx.MockTransport(_handle))
    assert await provider.digest("p", "m") == "digested"


async def test_ollama_provider_non_200_returns_none():
    provider = dp.OllamaProvider(transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    assert await provider.digest("p", "m") is None


async def test_ollama_provider_network_error_returns_none():
    def _raise(request: httpx.Request) -> httpx.Response:
        msg = "refused"
        raise httpx.ConnectError(msg, request=request)

    provider = dp.OllamaProvider(transport=httpx.MockTransport(_raise))
    assert await provider.digest("p", "m") is None


# ------------------------------------------------------------------ ClaudeSubscriptionProvider


async def test_claude_subscription_provider_falls_back_when_cli_missing(monkeypatch):
    monkeypatch.setenv("PATH", "/nonexistent")
    provider = dp.ClaudeSubscriptionProvider()
    assert await provider.digest("prompt", "claude-sonnet-5") is None


async def test_claude_subscription_provider_always_passes_bare(tmp_path):
    """The whole incident: without --bare, the spawned process re-triggers this repo's hooks."""
    argv_file = tmp_path / "argv.json"
    shim = tmp_path / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        f"python3 -c \"import json,sys; json.dump(sys.argv[1:], open('{argv_file}', 'w'))\" \"$@\"\n"
        'echo "digested output"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    provider = dp.ClaudeSubscriptionProvider(argv=[str(shim)])
    result = await provider.digest("prompt text", "claude-sonnet-5")

    assert result == "digested output"
    argv = json.loads(argv_file.read_text())
    assert "--bare" in argv
    assert "prompt text" in argv


async def test_claude_subscription_provider_nonzero_exit_returns_none(tmp_path):
    shim = tmp_path / "claude"
    shim.write_text("#!/bin/sh\nexit 1\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    provider = dp.ClaudeSubscriptionProvider(argv=[str(shim)])
    assert await provider.digest("p", "m") is None


async def test_claude_subscription_provider_timeout_kills_process(tmp_path):
    shim = tmp_path / "claude"
    shim.write_text("#!/bin/sh\nsleep 10\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    provider = dp.ClaudeSubscriptionProvider(argv=[str(shim)], timeout_s=0.2)
    assert await provider.digest("p", "m") is None
