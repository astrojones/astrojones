"""Pluggable backends for capture.py's digest-on-drain: one interface, three swappable providers.

Incident 2026-07-13 / re-incident 2026-07-16: digesting a capture batch spawns a Claude
process in this repo; if that process loads this repo's own hooks, its Stop hook re-enqueues
a row into the queue it just drained — a self-feeding loop that spawned ~1730 full Claude Code
processes, each paying a fresh cold start against the Anthropic subscription. The 07-13 fix
rested on one premise: ``ClaudeAgentSdkProvider`` omits ``setting_sources``, whose SDK default
"loads no user settings, plugins, or hooks." That premise was FALSE — omitting the flag makes
the SDK send no ``--setting-sources`` at all, and the CLI's own default then loads user +
project settings (hence hooks). The loop came back through the SDK path.

Defense-in-depth now, so no single false assumption can re-open it:
  1. Source cut (strongest): every digest spawn (SDK and ``--bare`` CLI) sets the
     ``DIGEST_SUBPROCESS_ENV`` sentinel in the child's environment; ``capture.enqueue`` is a
     hard no-op whenever that sentinel is present, so a digest subprocess cannot write to the
     queue regardless of which hooks load.
  2. Actually suppress hooks: ``ClaudeAgentSdkProvider`` passes ``setting_sources=[]``
     explicitly (empty list → ``--setting-sources=`` → load nothing), not by omission.
  3. Drain circuit-breaker (capture.py): a per-batch floor + per-minute cap so even a future
     re-feed cannot run away.
The SDK's value here is structured output (typed observations instead of free text), not
availability redundancy: when the SDK itself is broken/absent it delegates to the ``--bare``
CLI, and anything else fails open to shipping raw entries.

Swap backends with ``BRAIN_DIGEST_PROVIDER`` = ``claude-sdk`` (default) | ``openrouter`` |
``ollama`` | ``claude`` | ``off``. ``BRAIN_DIGEST_MODEL`` overrides the model id within
whichever provider is active.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Literal, Protocol, get_args

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

PROVIDER_ENV = "BRAIN_DIGEST_PROVIDER"
PROVIDER_DEFAULT = "claude-sdk"
MODEL_ENV = "BRAIN_DIGEST_MODEL"

# Leg 1 of the self-feed defense: set in every digest-spawned child's environment (SDK and
# CLI). ``capture.enqueue`` hard-drops when this is present, so a digest subprocess — and any
# hook it spawns, which inherits the env — can never write back into the queue it is draining,
# regardless of which hooks load. capture.py reads this name; keep them in sync.
DIGEST_SUBPROCESS_ENV = "REPO_AGENT_HARNESS_IN_DIGEST"

MODEL_DEFAULTS = {
    "claude-sdk": "claude-sonnet-5",
    # gemini-2.5-flash-lite and a bare gemini-3-flash-lite are both retired/nonexistent on
    # OpenRouter as of 2026-07; 3.1-flash-lite is the current cheap-lite Gemini (verified
    # against openrouter.ai/api/v1/models: $0.25/M prompt, $1.50/M completion, 1M context).
    "openrouter": "google/gemini-3.1-flash-lite",
    # Ollama's cloud-hosted Gemini 3 Flash — no local weights/GPU needed, just a running
    # `ollama serve` signed in to Ollama's cloud (verified against ollama.com/library).
    "ollama": "gemini-3-flash-preview:cloud",
    "claude": "claude-sonnet-5",
}

_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
_OPENROUTER_KEY_ENV = "OPENROUTER_API_KEY"
# Zero-setup fallback: reuse the key already configured for claude-code-router, if present.
_CCR_CONFIG_PATH = Path.home() / ".claude-code-router" / "config.json"

_OLLAMA_BASE_URL_ENV = "OLLAMA_BASE_URL"
_OLLAMA_BASE_URL_DEFAULT = "http://localhost:11434"

_DEFAULT_TIMEOUT_S = 120.0

# Closed vocabularies: observation types and concepts become node_set tags (type:<t>,
# concept:<c>) in the graph, so free-form values would fragment recall.
ObservationType = Literal["discovery", "feature", "change", "bugfix", "decision", "refactor", "security_alert"]
_OBSERVATION_TYPES: tuple[str, ...] = get_args(ObservationType)
_CONCEPT_VOCABULARY: tuple[str, ...] = (
    "how-it-works",
    "pattern",
    "what-changed",
    "gotcha",
    "problem-solution",
    "why-it-exists",
    "trade-off",
)

_DIGEST_PROMPT = (
    "You are compressing an agent-session capture log into durable memory observations. "
    "Summarize what was worked on, decisions made, files touched, and outcomes; keep "
    "concrete identifiers (paths, commits, tool names). Reply with ONLY a JSON object: "
    '{"observations": [{"type": "...", "title": "...", "facts": ["..."], '
    '"concepts": ["..."], "files": ["..."]}]}. '
    f"type is one of: {', '.join(_OBSERVATION_TYPES)}. "
    f"concepts is a subset of: {', '.join(_CONCEPT_VOCABULARY)}. "
    "facts are short standalone statements; files are repo-relative paths. "
    "No prose outside the JSON.\n\nEntries:\n"
)


class DigestObservation(BaseModel):
    """One typed memory observation distilled from a capture batch."""

    type: ObservationType
    title: str
    facts: list[str] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)
    files: list[str] = Field(default_factory=list)

    @field_validator("concepts")
    @classmethod
    def _known_concepts_only(cls, value: list[str]) -> list[str]:
        # Dropped, not rejected: one hallucinated concept must not void an otherwise
        # valid observation (the whole reply would fall back to plaintext).
        return [c for c in value if c in _CONCEPT_VOCABULARY]


class DigestResult(BaseModel):
    """Parsed digest reply: typed observations when the JSON validated, else the verbatim text."""

    observations: list[DigestObservation] | None = None
    text: str | None = None


# JSON schema handed to the SDK's structured-output mechanism (ClaudeAgentOptions.output_format).
_OBSERVATIONS_SCHEMA = {
    "type": "object",
    "properties": {"observations": {"type": "array", "items": DigestObservation.model_json_schema()}},
    "required": ["observations"],
}


def _parse_reply(raw: str) -> DigestResult:
    """Parse a provider reply into observations; fail open to plaintext on anything unusable."""
    text = raw.strip()
    if text.startswith("```") and text.endswith("```"):
        # Models love fencing JSON despite instructions; strip ``` / ```json wrappers.
        text = text[3:-3]
        text = text.split("\n", 1)[1] if "\n" in text else text
    try:
        payload = json.loads(text)
    except ValueError:
        return DigestResult(text=raw)
    observations = payload.get("observations") if isinstance(payload, dict) else None
    if not isinstance(observations, list) or not observations:
        return DigestResult(text=raw)
    try:
        parsed = [DigestObservation.model_validate(item) for item in observations]
    except ValidationError:
        return DigestResult(text=raw)
    return DigestResult(observations=parsed)


class DigestProvider(Protocol):
    """Anything that can turn a digest prompt into a summary string, or fail closed to None."""

    async def digest(self, prompt: str, model: str) -> str | None:
        """Return the digest text, or None on any failure (network, timeout, bad response)."""


@dataclass
class OpenRouterProvider:
    """Plain HTTP call to OpenRouter. No local process, so no local hooks to re-trigger."""

    api_key: str
    timeout_s: float = _DEFAULT_TIMEOUT_S
    transport: httpx.AsyncBaseTransport | None = None

    async def digest(self, prompt: str, model: str) -> str | None:
        """POST to OpenRouter's chat completions endpoint; None on any transport/shape failure."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
                resp = await http.post(
                    _OPENROUTER_URL,
                    headers={"Authorization": f"Bearer {self.api_key}"},
                    json={"model": model, "messages": [{"role": "user", "content": prompt}]},
                )
        except httpx.HTTPError:
            return None
        if resp.status_code != HTTPStatus.OK:
            return None
        try:
            text = resp.json()["choices"][0]["message"]["content"].strip()
        except (KeyError, IndexError, TypeError, ValueError):
            return None
        return text or None


@dataclass
class OllamaProvider:
    """Local Ollama server — fully offline, no subscription or API cost at all."""

    base_url: str = _OLLAMA_BASE_URL_DEFAULT
    timeout_s: float = _DEFAULT_TIMEOUT_S
    transport: httpx.AsyncBaseTransport | None = None

    async def digest(self, prompt: str, model: str) -> str | None:
        """POST to a local (or cloud-backed) Ollama server's chat endpoint; None on failure."""
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as http:
                resp = await http.post(
                    f"{self.base_url}/api/chat",
                    json={"model": model, "messages": [{"role": "user", "content": prompt}], "stream": False},
                )
        except httpx.HTTPError:
            return None
        if resp.status_code != HTTPStatus.OK:
            return None
        try:
            text = resp.json()["message"]["content"].strip()
        except (KeyError, TypeError, ValueError):
            return None
        return text or None


@dataclass
class ClaudeSubscriptionProvider:
    """The locally-authenticated ``claude`` CLI in print mode (draws on the Claude Max quota).

    Always passed ``--bare``: without it, the spawned process loads this repo's own hooks —
    see the incident note at the top of this module.
    """

    timeout_s: float = _DEFAULT_TIMEOUT_S
    argv: list[str] = field(default_factory=lambda: ["claude"])

    async def digest(self, prompt: str, model: str) -> str | None:
        """Shell out to the CLI in bare print mode; None on missing CLI, timeout, or non-zero exit."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *self.argv,
                "-p",
                prompt,
                "--model",
                model,
                "--output-format",
                "text",
                "--bare",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL,
                # Leg 1 source-cut: the sentinel makes capture.enqueue a no-op in this child
                # and any hook it spawns, so a digest process cannot re-feed the queue.
                env={**os.environ, DIGEST_SUBPROCESS_ENV: "1"},
            )
        except OSError:
            return None  # CLI not installed — raw entries it is
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self.timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            return None
        if proc.returncode != 0:
            return None
        text = out.decode("utf-8", errors="replace").strip()
        return text or None


@dataclass
class ClaudeAgentSdkProvider:
    """One-shot ``claude_agent_sdk.query()`` digest — the default backend.

    ``ClaudeAgentOptions`` is built WITHOUT ``setting_sources``: the SDK default loads no
    user settings, plugins, or hooks, so the 2026-07-13 self-feeding stop-hook incident
    class is structurally impossible here. The SDK is chosen for structured output, not
    availability redundancy — a broken/absent SDK or CLI delegates to ``fallback`` (the
    ``--bare`` CLI by default); every other failure, including timeout, fails open to None
    so the drain ships raw entries as before.
    """

    timeout_s: float = _DEFAULT_TIMEOUT_S
    fallback: DigestProvider | None = field(default_factory=ClaudeSubscriptionProvider)

    async def digest(self, prompt: str, model: str) -> str | None:
        """Query the SDK once; delegate to the fallback only on SDK/CLI-broken errors."""
        try:
            import claude_agent_sdk as sdk  # noqa: PLC0415 - lazy: keep import cost off the drain's happy path
        except ImportError:
            return await self._delegate(prompt, model)
        try:
            text = await asyncio.wait_for(self._query(sdk, prompt, model), self.timeout_s)
        except (sdk.CLINotFoundError, sdk.ProcessError, sdk.CLIJSONDecodeError):
            # The SDK's own transport is broken/absent; the plain CLI may still answer.
            return await self._delegate(prompt, model)
        except Exception:  # noqa: BLE001 - incl. timeout: digesting is best-effort, ship raw entries
            return None
        return text

    async def _delegate(self, prompt: str, model: str) -> str | None:
        return await self.fallback.digest(prompt, model) if self.fallback is not None else None

    @staticmethod
    async def _query(sdk, prompt: str, model: str) -> str | None:  # noqa: ANN001 - lazy module, typed as Any
        # setting_sources=[] (Leg 2): an empty list makes the SDK emit `--setting-sources=`,
        # which loads NO user/project settings, plugins, or hooks. Omitting it does the
        # opposite — the CLI then falls back to its own user+project default and loads hooks,
        # the 2026-07-16 re-incident. env sentinel (Leg 1) is independent belt-and-braces:
        # even if a hook somehow loads, capture.enqueue drops in a sentinel-marked child.
        options = sdk.ClaudeAgentOptions(
            model=model,
            max_turns=1,
            setting_sources=[],
            env={DIGEST_SUBPROCESS_ENV: "1"},
            output_format={"type": "json_schema", "schema": _OBSERVATIONS_SCHEMA},
        )
        async for message in sdk.query(prompt=prompt, options=options):
            if isinstance(message, sdk.ResultMessage):
                if message.structured_output is not None:
                    return json.dumps(message.structured_output)
                text = (message.result or "").strip()
                return text or None
        return None


def _openrouter_api_key() -> str | None:
    """``OPENROUTER_API_KEY``, else the key already configured for claude-code-router, else None."""
    key = os.environ.get(_OPENROUTER_KEY_ENV)
    if key:
        return key
    try:
        config = json.loads(_CCR_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    for provider in config.get("Providers", []):
        if provider.get("name") == "openrouter":
            return provider.get("api_key") or None
    return None


def selected_provider_name() -> str | None:
    """The provider name from ``BRAIN_DIGEST_PROVIDER`` (default openrouter), or None when 'off'."""
    raw = os.environ.get(PROVIDER_ENV)
    raw = PROVIDER_DEFAULT if raw is None else raw
    raw = raw.strip().lower()
    return None if raw in {"off", "0", "false", ""} else raw


def selected_model(provider_name: str) -> str:
    """The model id from ``BRAIN_DIGEST_MODEL``, else the default for ``provider_name``."""
    return (os.environ.get(MODEL_ENV) or MODEL_DEFAULTS.get(provider_name, "")).strip()


def build_provider(provider_name: str) -> DigestProvider | None:
    """Construct the named provider, or None if it can't be used (e.g. no OpenRouter key)."""
    if provider_name == "openrouter":
        api_key = _openrouter_api_key()
        return OpenRouterProvider(api_key=api_key) if api_key else None
    if provider_name == "ollama":
        return OllamaProvider(base_url=os.environ.get(_OLLAMA_BASE_URL_ENV) or _OLLAMA_BASE_URL_DEFAULT)
    if provider_name == "claude":
        return ClaudeSubscriptionProvider()
    if provider_name == "claude-sdk":
        return ClaudeAgentSdkProvider()
    return None


async def digest(entries: list[str]) -> DigestResult | None:
    """Digest raw capture entries via the configured provider; None means ship them raw."""
    if not entries:
        return None
    provider_name = selected_provider_name()
    if provider_name is None:
        return None
    provider = build_provider(provider_name)
    if provider is None:
        return None
    model = selected_model(provider_name)
    if not model:
        return None
    prompt = _DIGEST_PROMPT + "\n".join(entries)
    reply = await provider.digest(prompt, model)
    return _parse_reply(reply) if reply else None
