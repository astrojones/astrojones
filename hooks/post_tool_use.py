#!/usr/bin/env python3
"""PostToolUse nudge: after an edit/write, remind the agent to verify the change.

Static text, stdlib-only, no harness dependency (not worth a subprocess round-trip).
Always exits 0.
"""

from __future__ import annotations

import json
import sys

_EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_NUDGE = (
    "A file was modified. Before continuing, verify the change: run repo_verify_changed "
    "(or agent/tools/safe-diff then agent/tools/test-changed) to check only what changed."
)


def main() -> None:
    try:
        data = json.load(sys.stdin)
    except Exception:
        print(json.dumps({}))
        sys.exit(0)

    if data.get("tool_name", "") in _EDIT_TOOLS:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "PostToolUse",
                        "additionalContext": _NUDGE,
                    }
                }
            )
        )
    else:
        print(json.dumps({}))
    sys.exit(0)


if __name__ == "__main__":
    main()
