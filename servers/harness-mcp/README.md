# repo-agent-harness (MCP server + CLI)

The deterministic core of the **repo-agent-harness** plugin: one Python package that
powers both an MCP server and a command-line tool. Everything is repo-aware (operates on
the git repository at the current working directory), bounded (timeouts + output caps), and
safe (secret redaction, secret-path refusal, no path traversal).

## Install / run

```bash
# Run the MCP server (stdio). --project keeps the cwd at the *user's* repo:
uv run --project /path/to/mcp repo-agent-harness-mcp

# Run the CLI (same logic, terminal/CI face):
uv run --project /path/to/mcp repo-agent-harness overview
uv run --project /path/to/mcp repo-agent-harness check-command "rm -rf build"
```

> Use `--project`, not `--directory`: the server detects the repo from its working
> directory, so the cwd must stay at the target repo while uv resolves this package's deps.

## MCP tools

`repo.context.overview` · `repo.context.status` · `repo.context.relevant_files` ·
`repo.search.text` · `repo.search.files` · `repo.read.range` · `repo.impact.file` ·
`repo.verify.changed` · `repo.diff.current` · `repo.policy.check_command`

Resources expose the overview and the `agent/policies/*.yml`; prompts expose the
bugfix/feature/refactor/test workflows.

## CLI subcommands

`overview` · `status` · `relevant-files <task>` · `search-text <pattern> [paths...]` ·
`search-files <glob>` · `read-range <path> --start --end` · `impact <path>` ·
`verify-changed` · `diff` · `check-command <command>`

All emit JSON.

## Develop / test

```bash
uv run --directory mcp pytest -q
```

## Assumptions

- Heuristic tools (`relevant_files`, `impact`) are explicitly labelled; symbol-level
  intelligence is delegated to Serena.
- Shell policy is conservative by default; tune it in `agent/policies/shell.yml`.
- Built on the official `mcp` Python SDK (FastMCP).
