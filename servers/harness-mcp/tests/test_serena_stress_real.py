"""Tier-2: real-Serena concurrency stress gate (opt-in, real TypeScript language server).

Tier-1 (``test_serena_stress.py``) proves the *gateway* contract against an LSP-free fake: one
call's timeout or cancellation must not tear the shared Serena session out from under concurrent
siblings. It cannot answer the other half of the original flakiness report — *is the real
TypeScript language server itself concurrency-hostile?* If real ``tsserver`` mishandled
concurrent requests server-side, healthy parallel calls would still fail even with the gateway
fixed, and the follow-up would be the opposite of Tier-1's fix: a gateway semaphore serializing
forwards in :meth:`SerenaGateway.call`.

This test drives a REAL :class:`SerenaGateway` child (real Serena + real
``typescript-language-server``) under concurrent HEALTHY calls and asserts the fix holds:
**exactly one connect (the cold boot) and zero call failures**. A respawn or a failed healthy
call is the discriminator that would implicate server-side concurrency-hostility.

It is opt-in and environment-gated (real LSP, slow cold boot), so it never runs in the normal/CI
fast lane — set ``REPO_AGENT_HARNESS_SERENA_REAL_STRESS=1`` (with serena +
``typescript-language-server`` + ``node`` available) to run it before a release.
"""

import asyncio
import contextlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from repo_agent_harness import gateway

# --- opt-in gate ----------------------------------------------------------------------------
_OPT_IN = "REPO_AGENT_HARNESS_SERENA_REAL_STRESS"
_serena = gateway.serena_command()
_have_serena = (os.sep in _serena and Path(_serena).exists()) or shutil.which("serena") is not None
_have_tsserver = shutil.which("typescript-language-server") is not None and shutil.which("node") is not None
_runnable = bool(os.environ.get(_OPT_IN)) and _have_serena and _have_tsserver

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(
        not _runnable,
        reason=f"set {_OPT_IN}=1 with serena + typescript-language-server + node to run the real-LSP gate",
    ),
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _run(args, cwd):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def ts_repo(tmp_path: Path) -> Path:
    """A small, committed, TypeScript git repo with real symbols and a tsserver project config.

    Must be a git repo (Serena language detection reads ``git.list_files``) and must pre-seed
    ``.serena/project.yml`` directly: ``server._seed_serena_languages`` only runs at server
    startup and is not triggered by constructing the gateway, so without this Serena would
    auto-detect a single language and might never start tsserver. ``tsconfig.json`` +
    ``package.json`` put tsserver in configured-project mode, removing a server-side flakiness
    variable so any failure cleanly implicates concurrency rather than project-mode ambiguity.
    """
    _run(["git", "init", "-q"], tmp_path)
    _run(["git", "config", "user.email", "t@t.t"], tmp_path)
    _run(["git", "config", "user.name", "t"], tmp_path)
    src = tmp_path / "src"
    src.mkdir()
    (src / "calc.ts").write_text(
        "export function add(a: number, b: number): number {\n"
        "  return a + b;\n"
        "}\n\n"
        "export class Calculator {\n"
        "  value = 0;\n"
        "  increment(): void {\n"
        "    this.value += 1;\n"
        "  }\n"
        "}\n"
    )
    (src / "greet.ts").write_text("export function greet(name: string): string {\n  return `hi ${name}`;\n}\n")
    (tmp_path / "tsconfig.json").write_text(
        json.dumps({"compilerOptions": {"target": "ES2020", "module": "ESNext", "strict": True}, "include": ["src"]})
    )
    (tmp_path / "package.json").write_text(json.dumps({"name": "ts-stress-fixture", "version": "0.0.0"}))
    serena_dir = tmp_path / ".serena"
    serena_dir.mkdir()
    (serena_dir / "project.yml").write_text(
        yaml.safe_dump({"project_name": tmp_path.name, "languages": ["typescript"]}, sort_keys=False),
        encoding="utf-8",
    )
    _run(["git", "add", "-A"], tmp_path)
    _run(["git", "commit", "-qm", "init"], tmp_path)
    return tmp_path


def _haystack(result) -> str:
    """All text the result carries — text content blocks plus serialized structured content."""
    parts = [getattr(block, "text", "") or "" for block in (result.content or [])]
    if result.structuredContent:
        parts.append(json.dumps(result.structuredContent))
    return "\n".join(parts)


# Each entry: (tool, arguments, symbol name that MUST appear in a healthy result).
# Real Serena arg names — `name_path_pattern` / `relative_path` — NOT the fake's `name_path`:
# the wrong key returns isError=True and would masquerade as tsserver concurrency-hostility.
_CALLS = [
    ("find_symbol", {"name_path_pattern": "add", "relative_path": "src/calc.ts"}, "add"),
    ("find_symbol", {"name_path_pattern": "Calculator", "relative_path": "src/calc.ts"}, "Calculator"),
    ("find_symbol", {"name_path_pattern": "increment", "relative_path": "src/calc.ts"}, "increment"),
    ("find_symbol", {"name_path_pattern": "greet", "relative_path": "src/greet.ts"}, "greet"),
    ("get_symbols_overview", {"relative_path": "src/calc.ts"}, "Calculator"),
    ("get_symbols_overview", {"relative_path": "src/greet.ts"}, "greet"),
]


@pytest.mark.timeout(300)  # ceiling above connect (120s default) + cold multi-LSP boot + rounds
async def test_healthy_concurrent_real_serena_calls_never_respawn(ts_repo, monkeypatch):
    """Concurrent HEALTHY calls against a real tsserver trigger no respawn and no failure.

    The discriminator the whole tier exists for: with the gateway fix in place, the only thing
    left that could break healthy parallel TypeScript calls is server-side tsserver
    concurrency-hostility. If that existed it would show here as a respawn (a healthy call
    timed out and tripped the wedge reaper) or a failed/empty result. A clean pass means the
    reported TS flakiness is gone end-to-end; a failure points straight at the gateway-semaphore
    follow-up named in the assertion messages.
    """
    monkeypatch.setenv(gateway.SERENA_TIMEOUT_ENV, "30")  # generous per-call: a true hang fails inside the marker
    gw = gateway.SerenaGateway(str(ts_repo), transport=None)  # transport=None -> real Serena child

    # Count every connect/respawn through the single choke point, installed BEFORE the cold boot.
    connects = 0
    original_open_locked = gw._open_locked

    async def counting_open_locked():
        nonlocal connects
        connects += 1
        return await original_open_locked()

    monkeypatch.setattr(gw, "_open_locked", counting_open_locked)

    failures: list[str] = []
    try:
        # Warm-up: pay the cold tsserver boot once (this is the single expected connect).
        warm = await gw.call("get_symbols_overview", {"relative_path": "src/calc.ts"})
        assert not warm.isError, f"cold get_symbols_overview errored: {_haystack(warm)[:300]}"
        assert "Calculator" in _haystack(warm), "warm-up did not return the expected TS symbols"

        async def one(tool: str, args: dict, expected: str) -> None:
            try:
                res = await gw.call(tool, args)
            except Exception as exc:  # noqa: BLE001 — record, don't abort the round
                failures.append(f"{tool}{args}: raised {exc!r}")
                return
            if res.isError:
                failures.append(f"{tool}{args}: isError, content={_haystack(res)[:200]}")
            elif expected not in _haystack(res):
                failures.append(f"{tool}{args}: {expected!r} missing from result")

        for _ in range(5):  # 5 rounds x 6 concurrent healthy calls on the shared real session
            await asyncio.gather(*(one(tool, dict(args), expected) for tool, args, expected in _CALLS))

        assert connects == 1, (
            f"the shared Serena session respawned {connects - 1}x under healthy concurrent load — real "
            "tsserver appears concurrency-hostile (a healthy call timed out/failed and tripped the wedge "
            "reaper). Follow-up fix: a gateway semaphore serializing forwards in SerenaGateway.call."
        )
        assert gw._consecutive_timeouts == 0, "wedge counter advanced under healthy load — a healthy call timed out"
        assert not failures, (
            f"{len(failures)} healthy concurrent calls failed against the real tsserver: {failures[:5]} — this "
            "discriminates server-side concurrency-hostility from the (already-fixed) gateway teardown bug; if "
            "non-empty, the follow-up is a gateway semaphore."
        )
    finally:
        await gw.aclose()  # primary: terminates the real child + its language servers
        # Cross-version-orphan defense only — a near-no-op for this same-version child
        # (_is_stale_serena_child returns False when our own serena is in the cmdline).
        with contextlib.suppress(Exception):
            gateway.reap_stale_serena_children(str(ts_repo))
