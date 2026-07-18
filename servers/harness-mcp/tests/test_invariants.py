"""CI invariant checks for the claude-mem/cognee coexistence contract.

Most invariants have a behavioral home already:
- I1 (claude-mem opened read-only, never written) — ``test_claude_mem_reader`` (write-rejection +
  source scan of the reader).
- I3 (every write = remember + datasets/status verify + ledger row), I5 (writes only to
  ``cm_*`` datasets via the sanitiser), I6 (fail-open) — ``test_cognee_sync``; recall fail-open —
  ``test_hooks``.

I2 has no behavioral home: it is the *structural* guarantee that nothing on the memory path can
reach an LLM SDK or spawn a process. It is asserted here as a source scan across every
memory-path module — the single most load-bearing invariant of the contract.
"""

from pathlib import Path

from repo_agent_harness import (
    agent_hooks,
    capture,
    claude_mem_reader,
    cognee_client,
    cognee_sync,
    mem,
)

# The full memory path: the hooks that recall, the HTTP client, the read-only reader, the sync
# loop, and the mem tool layer. None of these may import the agent SDK or spawn a subprocess.
_MEMORY_PATH_MODULES = (agent_hooks, capture, claude_mem_reader, cognee_client, cognee_sync, mem)


def test_i2_no_sdk_or_subprocess_on_the_memory_path():
    """I2: no ``claude_agent_sdk`` import and no ``subprocess`` spawn anywhere on the memory path."""
    forbidden = ("claude_agent_sdk", "subprocess")
    for module in _MEMORY_PATH_MODULES:
        src = Path(module.__file__).read_text(encoding="utf-8")
        for token in forbidden:
            msg = f"{module.__name__} contains {token!r} — violates I2 (no LLM/subprocess on the memory path)"
            assert token not in src, msg
