#!/usr/bin/env bash
# Launcher for the bundled repo-agent-harness MCP server.
# $0 is resolved to the plugin cache directory by the shell, so dirname gives the plugin root.
#
# Dogfood override: when developing the harness itself, export
#   REPO_AGENT_HARNESS_DEV_ROOT=/path/to/your/astrojones/checkout
# (in your shell before launching Claude, or in the MCP server's `env` in settings) so the
# server — and every hook and verification it runs — executes your live working-tree source
# instead of the frozen plugin-cache snapshot. Falls back to the cache when unset/invalid.
root="$(dirname "$0")"
if [ -n "${REPO_AGENT_HARNESS_DEV_ROOT:-}" ] && [ -d "${REPO_AGENT_HARNESS_DEV_ROOT}/servers/harness-mcp" ]; then
    root="${REPO_AGENT_HARNESS_DEV_ROOT}"
fi
exec uv run --project "${root}/servers/harness-mcp" repo-agent-harness-mcp "$@"
