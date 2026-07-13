#!/usr/bin/env python3
"""PreCompact shim: pipe the event through the trusted bundled harness (capture enqueue).

The harness's ``agent_hooks.pre_compact`` does one local sqlite INSERT into the capture
queue — capturing state before context is squashed, with zero synchronous HTTP; the
in-server drain ships it to cognee later. Resolution + fail-open live in ``_harness_shim``.
"""

from __future__ import annotations

from _harness_shim import run

if __name__ == "__main__":
    run("pre-compact")
