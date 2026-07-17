"""Digest provider abstraction: config resolution, and each backend in isolation."""

import asyncio
import dataclasses
import json
import stat
import sys
import types

import httpx
import pytest
from repo_agent_harness import digest_providers as dp

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ------------------------------------------------------------- claude_agent_sdk test double


class _FakeCLINotFoundError(Exception):
    pass


class _FakeProcessError(Exception):
    pass


class _FakeCLIJSONDecodeError(Exception):
    pass


@dataclasses.dataclass
class _FakeResultMessage:
    structured_output: object = None
    result: str | None = None


def _install_fake_sdk(monkeypatch, *, messages=(), raises=None, delay=0.0, seen=None):
    """Stand-in claude_agent_sdk module: records the options built and drives query()'s outcome."""

    def _options(**kwargs):
        if seen is not None:
            seen["options"] = kwargs
        return kwargs

    async def _query(*, prompt, options):
        if seen is not None:
            seen["prompt"] = prompt
        if delay:
            await asyncio.sleep(delay)
        if raises is not None:
            raise raises
        for message in messages:
            yield message

    mod = types.SimpleNamespace(
        CLINotFoundError=_FakeCLINotFoundError,
        ProcessError=_FakeProcessError,
        CLIJSONDecodeError=_FakeCLIJSONDecodeError,
        ResultMessage=_FakeResultMessage,
        ClaudeAgentOptions=_options,
        query=_query,
    )
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", mod)
    return mod


class _RecordingFallback:
    def __init__(self):
        self.calls = []

    async def digest(self, prompt, model):
        self.calls.append((prompt, model))
        return "fallback digest"


# --------------------------------------------------------------------------- config


def test_selected_provider_name_defaults_to_claude_sdk(monkeypatch):
    monkeypatch.delenv(dp.PROVIDER_ENV, raising=False)
    assert dp.selected_provider_name() == "claude-sdk"


@pytest.mark.parametrize("off_value", ["off", "OFF", "0", "false", ""])
def test_selected_provider_name_off_switch(monkeypatch, off_value):
    monkeypatch.setenv(dp.PROVIDER_ENV, off_value)
    assert dp.selected_provider_name() is None


@pytest.mark.parametrize("backend", ["ollama", "openrouter", "claude"])
def test_selected_provider_name_switches_backend(monkeypatch, backend):
    monkeypatch.setenv(dp.PROVIDER_ENV, backend)
    assert dp.selected_provider_name() == backend


def test_selected_model_falls_back_to_provider_default(monkeypatch):
    monkeypatch.delenv(dp.MODEL_ENV, raising=False)
    assert dp.selected_model("openrouter") == dp.MODEL_DEFAULTS["openrouter"]
    assert dp.selected_model("ollama") == dp.MODEL_DEFAULTS["ollama"]
    assert dp.selected_model("claude-sdk") == "claude-sonnet-5"


def test_selected_model_env_override_wins(monkeypatch):
    monkeypatch.setenv(dp.MODEL_ENV, "some/other-model")
    assert dp.selected_model("openrouter") == "some/other-model"


def test_build_provider_unknown_name_is_none():
    assert dp.build_provider("not-a-real-provider") is None


def test_build_provider_claude_needs_no_credentials():
    assert isinstance(dp.build_provider("claude"), dp.ClaudeSubscriptionProvider)


def test_build_provider_claude_sdk_defaults_fallback_to_subscription_cli():
    provider = dp.build_provider("claude-sdk")
    assert isinstance(provider, dp.ClaudeAgentSdkProvider)
    assert isinstance(provider.fallback, dp.ClaudeSubscriptionProvider)


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
    monkeypatch.setenv(dp.PROVIDER_ENV, "openrouter")
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
    result = await dp.digest(["one", "two"])
    assert result == dp.DigestResult(text="digested")
    assert "one" in seen["prompt"]
    assert "two" in seen["prompt"]
    assert seen["model"] == dp.MODEL_DEFAULTS["openrouter"]


async def test_digest_parses_provider_json_into_observations(monkeypatch):
    payload = {
        "observations": [
            {
                "type": "bugfix",
                "title": "Fix drain crash",
                "facts": ["guarded None rows"],
                "concepts": ["gotcha"],
                "files": ["capture.py"],
            }
        ]
    }

    class _FakeProvider:
        async def digest(self, prompt, model):
            return json.dumps(payload)

    monkeypatch.setenv(dp.PROVIDER_ENV, "openrouter")
    monkeypatch.setattr(dp, "build_provider", lambda name: _FakeProvider())
    result = await dp.digest(["entry"])
    assert result is not None
    assert result.observations is not None
    (obs,) = result.observations
    assert obs == dp.DigestObservation.model_validate(payload["observations"][0])
    assert result.text is None


def test_digest_prompt_requests_the_observation_json_shape():
    assert '"observations"' in dp._DIGEST_PROMPT
    assert "security_alert" in dp._DIGEST_PROMPT
    assert "trade-off" in dp._DIGEST_PROMPT


# ----------------------------------------------------------------------- reply parsing


def test_parse_reply_roundtrips_valid_observations():
    payload = {
        "observations": [
            {
                "type": "decision",
                "title": "Pick sqlite",
                "facts": ["WAL survives concurrent writers"],
                "concepts": ["trade-off", "why-it-exists"],
                "files": ["repo_agent_harness/capture.py"],
            }
        ]
    }
    result = dp._parse_reply(json.dumps(payload))
    assert result.text is None
    assert result.observations is not None
    (obs,) = result.observations
    assert obs.type == "decision"
    assert obs.title == "Pick sqlite"
    assert obs.facts == ["WAL survives concurrent writers"]
    assert obs.concepts == ["trade-off", "why-it-exists"]
    assert obs.files == ["repo_agent_harness/capture.py"]


def test_parse_reply_strips_markdown_fence():
    payload = {"observations": [{"type": "change", "title": "t", "facts": [], "concepts": [], "files": []}]}
    fenced = "```json\n" + json.dumps(payload) + "\n```"
    result = dp._parse_reply(fenced)
    assert result.observations is not None
    assert result.observations[0].type == "change"


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        '{"observations": "not-a-list"}',
        '{"observations": [{"type": "not-a-real-type", "title": "t"}]}',
        '{"something": "else"}',
        '{"observations": []}',
    ],
)
def test_parse_reply_unusable_payload_falls_back_to_text(raw):
    """Fail-open: anything we cannot validate ships verbatim as a plaintext digest."""
    assert dp._parse_reply(raw) == dp.DigestResult(text=raw)


def test_observation_drops_unknown_concepts():
    """concept: node_set tags feed the graph — the vocabulary stays closed, extras are dropped."""
    obs = dp.DigestObservation(
        type="discovery",
        title="t",
        facts=[],
        concepts=["gotcha", "made-up-concept", "pattern"],
        files=[],
    )
    assert obs.concepts == ["gotcha", "pattern"]


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


async def test_claude_subscription_provider_always_passes_bare_and_sentinel(tmp_path):
    """The whole incident: without --bare, the spawned process re-triggers this repo's hooks.

    Also asserts the Leg 1 env sentinel reaches the child, so capture.enqueue no-ops even if a
    hook does load — belt-and-braces independent of --bare.
    """
    argv_file = tmp_path / "argv.json"
    env_file = tmp_path / "env.txt"
    shim = tmp_path / "claude"
    shim.write_text(
        "#!/bin/sh\n"
        f"python3 -c \"import json,sys; json.dump(sys.argv[1:], open('{argv_file}', 'w'))\" \"$@\"\n"
        f'printf "%s" "${dp.DIGEST_SUBPROCESS_ENV}" > "{env_file}"\n'
        'echo "digested output"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)

    provider = dp.ClaudeSubscriptionProvider(argv=[str(shim)])
    result = await provider.digest("prompt text", "claude-sonnet-5")

    assert result == "digested output"
    argv = json.loads(argv_file.read_text())
    assert "--bare" in argv
    assert "prompt text" in argv
    assert env_file.read_text() == "1"


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


# ------------------------------------------------------------------- ClaudeAgentSdkProvider


async def test_claude_sdk_provider_returns_structured_output_as_json(monkeypatch):
    seen = {}
    payload = {"observations": [{"type": "decision", "title": "t", "facts": [], "concepts": [], "files": []}]}
    _install_fake_sdk(
        monkeypatch,
        # A non-Result message first: the provider must skip it and read the ResultMessage.
        messages=[object(), _FakeResultMessage(structured_output=payload)],
        seen=seen,
    )
    provider = dp.ClaudeAgentSdkProvider(fallback=None)
    out = await provider.digest("prompt text", "claude-sonnet-5")
    assert out is not None
    assert json.loads(out) == payload
    assert seen["prompt"] == "prompt text"
    assert seen["options"]["model"] == "claude-sonnet-5"
    assert seen["options"]["max_turns"] == 1


async def test_claude_sdk_provider_suppresses_hooks_and_sets_sentinel(monkeypatch):
    """The self-feed guard (post 2026-07-16).

    Omitting setting_sources let the CLI load its own user+project default (hooks). Both legs
    must be present: setting_sources=[] (empty → ``--setting-sources=`` → load nothing) AND the
    env sentinel that makes enqueue a no-op.
    """
    seen = {}
    _install_fake_sdk(monkeypatch, messages=[_FakeResultMessage(result="x")], seen=seen)
    await dp.ClaudeAgentSdkProvider(fallback=None).digest("p", "m")
    assert seen["options"]["setting_sources"] == []
    assert seen["options"]["env"] == {dp.DIGEST_SUBPROCESS_ENV: "1"}


async def test_claude_sdk_provider_requests_json_schema_output(monkeypatch):
    seen = {}
    _install_fake_sdk(monkeypatch, messages=[_FakeResultMessage(result="x")], seen=seen)
    await dp.ClaudeAgentSdkProvider(fallback=None).digest("p", "m")
    assert seen["options"]["output_format"]["type"] == "json_schema"
    assert "observations" in json.dumps(seen["options"]["output_format"]["schema"])


async def test_claude_sdk_provider_uses_result_text_without_structured_output(monkeypatch):
    _install_fake_sdk(monkeypatch, messages=[_FakeResultMessage(result="  plain digest  ")])
    assert await dp.ClaudeAgentSdkProvider(fallback=None).digest("p", "m") == "plain digest"


@pytest.mark.parametrize("exc_cls", [_FakeCLINotFoundError, _FakeProcessError, _FakeCLIJSONDecodeError])
async def test_claude_sdk_provider_delegates_to_fallback_on_broken_sdk_or_cli(monkeypatch, exc_cls):
    _install_fake_sdk(monkeypatch, raises=exc_cls("boom"))
    fallback = _RecordingFallback()
    provider = dp.ClaudeAgentSdkProvider(fallback=fallback)
    assert await provider.digest("prompt text", "claude-sonnet-5") == "fallback digest"
    assert fallback.calls == [("prompt text", "claude-sonnet-5")]


async def test_claude_sdk_provider_delegates_to_fallback_when_sdk_missing(monkeypatch):
    # A None entry in sys.modules makes `import claude_agent_sdk` raise ImportError.
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    fallback = _RecordingFallback()
    assert await dp.ClaudeAgentSdkProvider(fallback=fallback).digest("p", "m") == "fallback digest"
    assert fallback.calls == [("p", "m")]


async def test_claude_sdk_provider_unexpected_error_fails_open_to_none(monkeypatch):
    """Anything outside the broken-SDK/CLI list ships raw entries — no fallback, no raise."""
    _install_fake_sdk(monkeypatch, raises=RuntimeError("boom"))
    fallback = _RecordingFallback()
    assert await dp.ClaudeAgentSdkProvider(fallback=fallback).digest("p", "m") is None
    assert fallback.calls == []


async def test_claude_sdk_provider_timeout_fails_open_to_none(monkeypatch):
    _install_fake_sdk(monkeypatch, messages=[_FakeResultMessage(result="late")], delay=5.0)
    fallback = _RecordingFallback()
    provider = dp.ClaudeAgentSdkProvider(timeout_s=0.05, fallback=fallback)
    assert await provider.digest("p", "m") is None
    assert fallback.calls == []


async def test_claude_sdk_provider_none_fallback_returns_none(monkeypatch):
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", None)
    assert await dp.ClaudeAgentSdkProvider(fallback=None).digest("p", "m") is None
