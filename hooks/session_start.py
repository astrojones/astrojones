#!/usr/bin/env python3
"""SessionStart shim: pipe the event through the trusted bundled harness (memory recall).

The harness's ``agent_hooks.session_start`` performs the ONE network-allowed hook call — a
bounded (default 3s), fail-open ``mem_search`` against the remote cognee graph — and injects
the recall as ``additionalContext``. Resolution + fail-open live in ``_harness_shim``.
"""

from __future__ import annotations

from _harness_shim import run

if __name__ == "__main__":
    run("session-start")
