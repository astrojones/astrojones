#!/usr/bin/env python3
"""PostToolUse shim: pipe the event through the trusted bundled harness (perception-aware).

The harness's ``agent_hooks.post_tool_use`` records the edited path and surfaces any current
background-check regression (staying quiet on green, falling back to the static verify nudge
when no perception snapshot exists yet). Resolution + fail-open live in ``_harness_shim``.
"""

from __future__ import annotations

from _harness_shim import run

if __name__ == "__main__":
    run("post-tool-use")
