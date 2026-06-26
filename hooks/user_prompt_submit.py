#!/usr/bin/env python3
"""UserPromptSubmit shim: inject the per-turn perception digest via the trusted bundled harness.

The harness's ``agent_hooks.user_prompt_submit`` reads the perception snapshot and emits only
deltas since the last turn (a check went red/recovered, an external branch/HEAD switch, new
conflicts), staying silent otherwise. Resolution + fail-open live in ``_harness_shim``.
"""

from __future__ import annotations

from _harness_shim import run

if __name__ == "__main__":
    run("user-prompt-submit")
