#!/usr/bin/env python3
"""Stop shim: pipe the event through the trusted bundled harness (capture enqueue).

The harness's ``agent_hooks.stop`` does one local sqlite INSERT into the capture queue —
zero synchronous HTTP; the in-server drain ships it to cognee later. Resolution +
fail-open live in ``_harness_shim``.
"""

from __future__ import annotations

from _harness_shim import run

if __name__ == "__main__":
    run("stop")
