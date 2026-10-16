# astrojones — opencode plugin

The opencode half of the dual-target `astrojones` harness plugin. Mirrors what the
Claude Code half does, except: opencode does not auto-load `.claude/`, so this
plugin **materializes** the per-assistant surfaces (skills, commands, agents)
into the locations opencode reads from.

One default export serves **both OpenCode 1 and OpenCode 2**: V1 uses the named
`AstrojonesDev` export (a plugin factory returning the V1 hook map), V2 reads the
default export's `{ id, setup }`. See [OpenCode 2](#opencode-2) below.

The harness MCP server's `prompts/<name>.md` files are the single source of
truth; this plugin derives the opencode surfaces from them on load.

## Install

### From npm (not yet published)

> The package is **not yet published** to npm. The name below matches
> `package.json` (`astrojones-opencode`); use the local-clone method until a release
> is cut.

Add to `~/.config/opencode/opencode.json` (global) or `./opencode.json` (project):

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "plugins": ["astrojones-opencode"]
}
```

On OpenCode 1, use the singular `plugin` key instead; V2 normalizes it, so the
same install works on both. opencode runs `bun install` at startup; the plugin's
`npm install` brings in the harness server's launcher and registers the
load-time bootstrap hook.

### From a local clone (development)

Symlink the plugin file into opencode's global plugin directory:

```bash
mkdir -p ~/.config/opencode/plugins
ln -s "$(pwd)/opencode/plugin/astrojones.ts" \
      ~/.config/opencode/plugins/astrojones.ts
```

Then add the harness MCP server to your `~/.config/opencode/opencode.json`:

```jsonc
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "servers": {
      "repo-agent-harness": {
        "type": "local",
        "command": [
          "uv", "run", "--project",
          "<path-to-astrojones>/servers/harness-mcp",
          "repo-agent-harness-mcp"
        ]
      }
    }
  }
}
```

On OpenCode 1, keep the server directly under `mcp` and mark it `"enabled": true`.
V2 accepts that V1 shape too, so either form loads on either release.

## What it does on load

1. **Bootstrap** — on plugin load (fire-and-forget) and on the first tool call
   (awaited), runs `repo-agent-harness bootstrap --target opencode` in the
   current worktree. This writes/merges `.opencode/opencode.json` with the
   harness server entry and the `skills.paths` sentinel.
2. **Skills materialization** — reads every `repo_agent_harness/prompts/<name>.md`
   and writes `<plugin>/opencode/skills/<name>/SKILL.md` with minimal opencode
   frontmatter (`name`, `description`, `compatibility`, `metadata`).
3. **`skills.paths` rewrite** — replaces the `<set-by-opencode-plugin>` sentinel
   in `.opencode/opencode.json` with the real path to `<plugin>/opencode/skills/`.
4. **Commands + agents translation** — copies astrojones's commands and agents into
   `.opencode/commands/` and `.opencode/agents/`, stripping Claude-only
   frontmatter keys (`color`, `allowed-tools`, `argument-hint`). astrojones carries
   only generic surfaces, so nothing is filtered (the exclusion sets in
   `plugin/astrojones.ts` are empty guards for the future).
5. **Drift check** — calls `repo-agent-harness drift-check`; if any plugin-shipped
   `skills/<name>/SKILL.md` has diverged from the harness body, logs a
   `console.warn` listing the drifted prompts. **Never blocks**, never errors.
6. **Command policy hook** — every `bash`/`Bash` shell call has its command
   forwarded to `repo-agent-harness check-command`. A harness deny blocks the
   call with a structured error; a "confirm-first" decision raises the
   interactive prompt instead. Mirror of the Claude `PreToolUse` hook; the
   wiring differs per API — see [OpenCode 2](#opencode-2).

## OpenCode 2

OpenCode 2 dropped the V1 plugin API: a factory returning a hook map no longer
loads (`Plugin must export a default definition with an id and an effect or setup
function`). This plugin keeps one entrypoint for both releases by default-exporting
an object with `server()` (V1) and `setup(ctx)` (V2):

- **Skills / commands / agents** are unchanged. Both APIs run the same
  materialization into `<target>/.opencode/{skills,commands,agents}` and rewrite
  the `skills.paths` sentinel.
- **Policy hook**: V2 replaces the V1 `tool.execute.before` + `permission.ask`
  pair with a single `ctx.permission.hook("evaluate")`. A V2 evaluate hook may set
  `effect` to `deny` as well as `ask`, so the harness check runs once and
  `resources` already carries the scanner-produced shell command — no `callID`
  join between hooks. A subprocess-free regex fallback stays registered on
  `ctx.tool.hook("execute.before")` as a belt-and-suspenders safety floor.
- The V2 context is typed structurally instead of importing `@opencode/plugin`, so
  the file keeps loading under both APIs with no extra runtime dependency.

## Fail-open

Every step is wrapped in a `try { … } catch { console.warn }`. A user without
the harness server (or without `uv` / a Python env on `PATH`) still gets a
working opencode — they just lose the prompt-drift check and the policy hook,
and the built-in destructive-command fallback regex blocks the obvious nukes
(`rm -rf /`, `git push --force`, `gh repo delete`, `chmod -R 777`,
`docker compose down -v`).

## Scope

astrojones is the **generic** harness — it ships only assistant-agnostic coding
surfaces. The astrojones org deploy layer (`nuklaut-deploy`, `deploy-doctor`,
`/new-app`, `/harness-app`) lives in the separate, private
[`deploy`](https://github.com/astrojones/deploy) plugin and is not part of astrojones
on either the Claude Code or the opencode side.

## How the two halves fit together

```
                harness MCP server (SSOT)
                ────────────────────────
   prompts/<name>.md  ◄────────►  @mcp.prompt() registration
   bootstrap          ◄────────►  CLI subcommand + repo_bootstrap tool
   drift              ◄────────►  repo_drift_check + repo_drift_sync tools
                       ▲
                       │  derived at install time
            ┌──────────┴──────────┐
            │                     │
  Claude Code half         opencode half
   • skills/<name>/SKILL.md  • opencode/plugin/astrojones.ts
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
