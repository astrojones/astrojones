"""Pluggable backends for capture.py's digest-on-drain: one interface, three swappable providers.

Incident 2026-07-13: the original implementation shelled out to the locally-authenticated
``claude`` CLI in print mode, without ``--bare``. That let the spawned process load this
repo's own hooks, so its own Stop hook re-enqueued a row into the queue it had just drained —
a self-feeding loop that spawned 2000+ full Claude Code processes in six hours, each paying a
fresh ~60k-token cold start (no cache reuse across processes), against the Anthropic
subscription. Fixed two ways: ``ClaudeSubscriptionProvider`` now always passes ``--bare``, and
it is no longer the default — ``BRAIN_DIGEST_PROVIDER`` picks the backend, defaulting to
``openrouter`` so routine digesting draws from a separate, low-stakes budget instead of the
Claude Max quota.

Swap backends with ``BRAIN_DIGEST_PROVIDER`` = ``openrouter`` (default) | ``ollama`` | ``claude``
| ``off``. ``BRAIN_DIGEST_MODEL`` overrides the model id within whichever provider is active.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from dataclasses import dataclass, field
from http import HTTPStatus
from pathlib import Path
from typing import Protocol

import httpx

PROVIDER_ENV = "BRAIN_DIGEST_PROVIDER"
PROVIDER_DEFAULT = "openrouter"
MODEL_ENV = "BRAIN_DIGEST_MODEL"

MODEL_DEFAULTS = {
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

_DIGEST_PROMPT = (
    "You are compressing an agent-session capture log into a durable memory observation. "
    "Summarize the entries below into a compact digest: what was worked on, decisions made, "
    "files touched, and outcomes. Keep concrete identifiers (paths, commits, tool names). "
    "Output only the digest text.\n\nEntries:\n"
)


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
    return None


async def digest(entries: list[str]) -> str | None:
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
    return await provider.digest(prompt, model)
