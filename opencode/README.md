# astrojones-dev — opencode plugin

The opencode half of the dual-target `astrojones-dev` plugin. Mirrors what the
Claude Code half does, except: opencode does not auto-load `.claude/` so the
plugin **materializes** the per-assistant surfaces (skills, commands, agents)
into the locations opencode reads from.

The harness MCP server's `prompts/<name>.md` files are the single source of
truth; this plugin derives the opencode surfaces from them on load.

## Install

### From npm (recommended once published)

Add to `~/.config/opencode/opencode.json` (global) or `./opencode.json` (project):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugin": ["@astrojones/opencode-plugin"]
}
```

opencode runs `bun install` at startup; the plugin's `npm install` brings in
the harness server's launcher and registers the load-time bootstrap hook.

### From a local clone (development)

Symlink the plugin file into opencode's global plugin directory:

```bash
mkdir -p ~/.config/opencode/plugins
ln -s "$(pwd)/opencode/plugin/astrojones-dev.ts" \
      ~/.config/opencode/plugins/astrojones-dev.ts
```

Then add the harness MCP server to your `~/.config/opencode/opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "repo-agent-harness": {
      "type": "local",
      "command": [
        "uv", "run", "--project",
        "<path-to-astrojones-dev>/servers/harness-mcp",
        "repo-agent-harness-mcp"
      ],
      "enabled": true
    }
  }
}
```

## What it does on load

1. **Bootstrap** — on plugin load (fire-and-forget) and on the first tool
   call (awaited), runs `repo-agent-harness bootstrap --target opencode`
   in the current worktree. This writes/merges `.opencode/opencode.json`
   with the harness server entry and the `skills.paths` sentinel.
2. **Skills materialization** — reads every `repo_agent_harness/prompts/<name>.md`
   and writes `<plugin>/opencode/skills/<name>/SKILL.md` with minimal opencode
   frontmatter (`name`, `description`, `compatibility`, `metadata`).
3. **`skills.paths` rewrite** — replaces the `<set-by-opencode-plugin>` sentinel
   in `.opencode/opencode.json` with the real path to `<plugin>/opencode/skills/`.
4. **Commands + agents translation** — copies the Claude-side commands and
   agents (minus the org-only ones — `/new-app`, `/harness-app`, `deploy-doctor`)
   into `.opencode/commands/` and `.opencode/agents/`, stripping Claude-only
   frontmatter keys (`color`, `allowed-tools`, `argument-hint`).
5. **Drift check** — calls `repo-agent-harness drift-check`; if any plugin-shipped
   `skills/<name>/SKILL.md` has diverged from the harness body, logs a
   `console.warn` listing the drifted prompts. **Never blocks**, never errors.
6. **`tool.execute.before` policy hook** — every `bash`/`Bash` tool call has its
   command forwarded to `repo-agent-harness check-command`. If the harness
   denies, the call is blocked with a structured error. Mirror of the Claude
   `PreToolUse` hook that PR #3 inherits via the `repo_policy_check_command`
   MCP tool.

## Fail-open

Every step is wrapped in a `try { … } catch { console.warn }`. A user without
the harness server (or without `uv` / a Python env on `PATH`) still gets a
working opencode — they just lose the prompt-drift check and the policy hook,
and the built-in destructive-command fallback regex blocks the obvious nukes
(`rm -rf /`, `git push --force`, `gh repo delete`, `chmod -R 777`,
`docker compose down -v`).

## What's NOT in the opencode plugin

Org-only knowledge stays Claude-only in `astrojones-dev`:

- The `nuklaut-deploy` skill.
- The `deploy-doctor` agent.
- The `/new-app` and `/harness-app` commands.

These are filtered out of the translation step on purpose. A separate org
plugin is the planned home for them.

## How the two halves fit together

```
                harness MCP server (SSOT)
                ────────────────────────
   prompts/<name>.md  ◄────────►  @mcp.prompt() registration
   bootstrap          ◄────────►  CLI subcommand + repo_bootstrap_status tool
   drift              ◄────────►  repo_drift_check + repo_drift_sync tools
                       ▲
                       │  derived at install time
            ┌──────────┴──────────┐
            │                     │
  Claude Code half         opencode half
  (PR #3, done)            (this plugin)
   • skills/<name>/SKILL.md  • opencode/plugin/astrojones-dev.ts
     (offline copy, drift-     (Plugin factory)
      checked)                • opencode/opencode.json
   • commands/harness-init.md   (plugin manifest)
   • hooks/pre_tool_use.py    • opencode/skills/  (derived at install)
                              • .opencode/commands/ + .opencode/agents/
                                (derived at install)
```

Both halves read the same SSOT; the harness server is the only place a prompt
body is edited.

## See also

- Top-level `README.md` for the Claude Code install snippet.
- `servers/harness-mcp/` for the SSOT prompts and the CLI/MCP surface.
- Closes: [#4](https://github.com/astrojones/astrojones-dev/issues/4)
- Depends on: [#3](https://github.com/astrojones/astrojones-dev/pull/3)
