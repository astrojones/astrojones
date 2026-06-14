#!/bin/bash
set -euo pipefail

# SessionStart hook to initialize Serena and repo context to project directory
# When running in cloud sessions, this ensures the repo-agent-harness starts
# with the correct working directory instead of defaulting to home directory

cd "$CLAUDE_PROJECT_DIR"

# Bootstrap repo context if needed
if [ ! -f ".claude/settings.json" ]; then
    echo "Bootstrapping repo context..."
fi

# Ensure we're in the right directory for any subsequent tool initialization
export CLAUDE_WORKING_DIR="$CLAUDE_PROJECT_DIR"

echo "✓ Session initialized in project directory: $CLAUDE_PROJECT_DIR"
