import type { Plugin } from "@opencode-ai/plugin"
import { spawn } from "node:child_process"
import { mkdir, readdir, readFile, stat, writeFile } from "node:fs/promises"
import { dirname, join, resolve } from "node:path"
import { fileURLToPath } from "node:url"

const HERE = dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = resolve(HERE, "..", "..")
const OPENCODE_DIR = resolve(HERE, "..")
const HARNESS_PROJECT = join(PLUGIN_ROOT, "servers", "harness-mcp")
const PROMPTS_DIR = join(HARNESS_PROJECT, "repo_agent_harness", "prompts")
const SKILLS_OUT = join(OPENCODE_DIR, "skills")

// Org-only commands/agents/skills now live in the separate `deploy` plugin, so
// astrojones carries none of them. These exclusion sets stay (empty) as the guard:
// any future non-generic entry MUST be listed here (by `<name>.md`) so it can
// never leak into the opencode skills/commands/agents tree via materialize*.
const CLAUDE_ONLY_COMMANDS = new Set<string>([])
const CLAUDE_ONLY_AGENTS = new Set<string>([])
const CLAUDE_ONLY_SKILLS = new Set<string>([])

export const DESTRUCTIVE_FALLBACK = [
  /\bgit\s+push\s+(?:[^|;]*\s)?(-f|--force)\b/,
  // rm with BOTH a recursive- and a force-indicator anywhere before the next
  // command separator: catches combined (-rf), separated (-r -f) and long
  // (--recursive --force) flags, while a plain `rm file.txt` (no flags) misses.
  /\brm\b(?=[^|;]*\s-(?:-recursive\b|[a-z]*r))(?=[^|;]*\s-(?:-force\b|[a-z]*f))/,
  /\bgh\s+repo\s+delete\b/,
  /\bchmod\s+-R\s+777\b/,
  /\bdocker\b[^|;]*\bdown\b[^|;]*\s-v\b/,
]

type BootstrapResult = {
  ok: boolean
  created?: string[]
  merged?: string[]
  skipped?: string[]
  error?: string
}

async function exec(
  command: string,
  args: string[],
  opts: { cwd?: string; input?: string; timeoutMs?: number } = {},
): Promise<{ code: number; stdout: string; stderr: string }> {
  return await new Promise((resolveExec) => {
    const child = spawn(command, args, {
      cwd: opts.cwd,
      stdio: ["pipe", "pipe", "pipe"],
    })
    let stdout = ""
    let stderr = ""
    const timer = opts.timeoutMs
      ? setTimeout(() => child.kill("SIGKILL"), opts.timeoutMs)
      : null
    child.stdout.on("data", (b: Buffer) => {
      stdout += b.toString("utf8")
    })
    child.stderr.on("data", (b: Buffer) => {
      stderr += b.toString("utf8")
    })
    child.on("error", () => {
      if (timer) clearTimeout(timer)
      resolveExec({ code: 1, stdout, stderr })
    })
    child.on("close", (code) => {
      if (timer) clearTimeout(timer)
      resolveExec({ code: code ?? 1, stdout, stderr })
    })
    if (opts.input !== undefined) child.stdin.end(opts.input)
    else child.stdin.end()
  })
}

async function runHarnessCli(
  args: string[],
  opts: { cwd?: string; timeoutMs?: number } = {},
): Promise<{ ok: boolean; out: unknown; stderr: string }> {
  const res = await exec(
    "uv",
    [
      "run",
      "--quiet",
      "--project",
      HARNESS_PROJECT,
      "repo-agent-harness",
      ...args,
    ],
    { cwd: opts.cwd, timeoutMs: opts.timeoutMs ?? 30_000 },
  )
  // NOTE: `ok` tracks exit code only. The harness denies a command via
  // exit 0 + JSON {"allowed": false}, so policyCheck honors structured
  // decisions in the `res.ok` branch. If the harness CLI ever starts
  // exiting non-zero on a deny, this would fall through to the weak
  // fallback regex — callers must keep that contract in mind.
  if (res.code !== 0 && !res.stdout) {
    return { ok: false, out: null, stderr: res.stderr }
  }
  try {
    return {
      ok: res.code === 0,
      out: JSON.parse(res.stdout || "{}"),
      stderr: res.stderr,
    }
  } catch {
    return { ok: false, out: null, stderr: res.stderr || res.stdout }
  }
}

async function exists(p: string): Promise<boolean> {
  try {
    await stat(p)
    return true
  } catch {
    return false
  }
}

async function materializeSkills(): Promise<void> {
  if (!(await exists(PROMPTS_DIR))) return
  await mkdir(SKILLS_OUT, { recursive: true })
  const entries = await readdir(PROMPTS_DIR)
  for (const file of entries) {
    if (!file.endsWith(".md") || file.startsWith("_")) continue
    if (CLAUDE_ONLY_SKILLS.has(file)) continue
    const name = file.slice(0, -3)
    const body = await readFile(join(PROMPTS_DIR, file), "utf8")
    const description = firstLine(body).replace(/^#+\s*/, "").trim() || name
    const out = join(SKILLS_OUT, name, "SKILL.md")
    await mkdir(dirname(out), { recursive: true })
    const frontmatter = [
      "---",
      `name: ${name}`,
      // JSON.stringify yields a valid YAML double-quoted scalar, escaping
      // colons, quotes and leading block indicators in the prompt's first line.
      `description: ${JSON.stringify(description)}`,
      "compatibility: opencode",
      "metadata:",
      "  source: repo-agent-harness:prompts",
      "---",
      "",
    ].join("\n")
    await writeFile(out, frontmatter + body, "utf8")
  }
}

export function firstLine(s: string): string {
  const i = s.indexOf("\n")
  return i === -1 ? s : s.slice(0, i)
}

async function materializeCommands(target: string): Promise<void> {
  const src = join(PLUGIN_ROOT, "commands")
  if (!(await exists(src))) return
  const dest = join(target, ".opencode", "commands")
  await mkdir(dest, { recursive: true })
  for (const file of await readdir(src)) {
    if (!file.endsWith(".md")) continue
    if (CLAUDE_ONLY_COMMANDS.has(file)) continue
    const body = await readFile(join(src, file), "utf8")
    await writeFile(join(dest, file), claudeToOpencode(body), "utf8")
  }
}

async function materializeAgents(target: string): Promise<void> {
  const src = join(PLUGIN_ROOT, "agents")
  if (!(await exists(src))) return
  const dest = join(target, ".opencode", "agents")
  await mkdir(dest, { recursive: true })
  for (const file of await readdir(src)) {
    if (!file.endsWith(".md")) continue
    if (CLAUDE_ONLY_AGENTS.has(file)) continue
    const body = await readFile(join(src, file), "utf8")
    await writeFile(join(dest, file), claudeToOpencode(body), "utf8")
  }
}

export function claudeToOpencode(body: string): string {
  const m = body.match(/^---\n([\s\S]*?)\n---\n([\s\S]*)$/)
  if (!m) return body
  const yaml = m[1] ?? ""
  const rest = m[2] ?? ""
  const kept: string[] = []
  const lines = yaml.split("\n")
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i] ?? ""
    if (/^(color|allowed-tools|argument-hint):/.test(line)) {
      // Drop the key line and any following more-indented continuation lines
      // (block scalar/sequence values) so block-style keys don't orphan.
      while (i + 1 < lines.length && /^\s+\S/.test(lines[i + 1] ?? "")) i++
      continue
    }
    kept.push(line)
  }
  return `---\n${kept.join("\n")}\n---\n${rest}`
}

// Sentinels the harness writes into a bootstrapped opencode.json; the plugin
// rewrites them to the absolute, materialized skills directory on load.
export const SKILLS_PATH_SENTINELS = [
  "<set-by-opencode-plugin>",
  "__HARNESS_OPENCODE_SKILLS_PATH__",
]

// Pure sentinel -> resolved-path mapping (no file I/O), extracted from
// rewriteOpencodeSkillsPath so it is unit-testable in isolation.
export function mapSkillsPaths(paths: string[], resolved: string): string[] {
  return paths.map((p) => (SKILLS_PATH_SENTINELS.includes(p) ? resolved : p))
}

async function rewriteOpencodeSkillsPath(target: string): Promise<void> {
  const cfgPath = join(target, ".opencode", "opencode.json")
  if (!(await exists(cfgPath))) return
  try {
    const raw = await readFile(cfgPath, "utf8")
    const cfg = JSON.parse(raw) as {
      skills?: { paths?: string[] }
    }
    if (!cfg.skills || !Array.isArray(cfg.skills.paths)) return
    const next = mapSkillsPaths(cfg.skills.paths, SKILLS_OUT)
    if (JSON.stringify(next) === JSON.stringify(cfg.skills.paths)) return
    cfg.skills.paths = next
    await writeFile(cfgPath, JSON.stringify(cfg, null, 2) + "\n", "utf8")
  } catch {
    // fail open
  }
}

async function checkDrift(target: string): Promise<void> {
  const res = await runHarnessCli(["drift-check"], { cwd: target })
  if (!res.ok || !res.out || typeof res.out !== "object") return
  const out = res.out as { drifted?: string[] }
  if (out.drifted && out.drifted.length > 0) {
    console.warn(
      `[astrojones] prompt drift detected for: ${out.drifted.join(
        ", ",
      )}. Run \`repo-agent-harness sync-prompts\` to refresh.`,
    )
  }
}

async function policyCheck(
  command: string,
  target: string,
): Promise<{ allowed: boolean; reason: string; requires_confirmation: boolean }> {
  try {
    const res = await runHarnessCli(["check-command", command], {
      cwd: target,
      timeoutMs: 5_000,
    })
    if (res.ok && res.out && typeof res.out === "object") {
      const o = res.out as {
        allowed?: boolean
        reason?: string
        requires_confirmation?: boolean
      }
      return {
        allowed: o.allowed !== false,
        reason: o.reason ?? "",
        requires_confirmation: !!o.requires_confirmation,
      }
    }
  } catch {
    // fall through
  }
  for (const re of DESTRUCTIVE_FALLBACK) {
    if (re.test(command)) {
      return {
        allowed: false,
        reason: `destructive command blocked by built-in fallback (${re})`,
        requires_confirmation: false,
      }
    }
  }
  return { allowed: true, reason: "no policy match (fail-open)", requires_confirmation: false }
}

let bootstrapped: Promise<void> | null = null

// Confirm-first tier linkage between the two hooks. `tool.execute.before` has
// the real command (so it runs the policy check) but opencode's before-hook is
// allow/deny only. When the harness returns requires_confirmation we record the
// tool call's `callID` here instead of throwing; `permission.ask` then turns
// that into an interactive "ask" prompt. Both hooks carry a typed `callID`, so
// the join never depends on parsing the opaque Permission.metadata.
//
// Firing-order assumption: tool.execute.before runs before permission.ask for
// the same callID. Verified by signature/field typing only -- opencode core is
// not vendored here, so it is not runtime-verified. The design degrades
// benignly: hard-denies still throw in tool.execute.before (the safety floor
// never depends on permission.ask firing); the worst case if ordering differs
// or permission.ask never fires is a confirm-first command runs unprompted -- a
// degraded tier, matching the plugin's documented fail-open stance.
const confirmFirstCallIDs = new Set<string>()

async function bootstrapOnce(target: string): Promise<void> {
  if (bootstrapped) return bootstrapped
  bootstrapped = (async () => {
    try {
      await materializeSkills()
    } catch (err) {
      console.warn(`[astrojones] materializeSkills failed: ${String(err)}`)
    }
    try {
      const res = await runHarnessCli(
        ["bootstrap", "--target", "opencode"],
        { cwd: target },
      )
      if (!res.ok) {
        console.warn(
          `[astrojones] bootstrap returned non-zero: ${
            (res.out as BootstrapResult | null)?.error ?? res.stderr
          }`,
        )
      }
    } catch (err) {
      console.warn(`[astrojones] bootstrap failed: ${String(err)}`)
    }
    try {
      await rewriteOpencodeSkillsPath(target)
    } catch (err) {
      console.warn(
        `[astrojones] rewriteOpencodeSkillsPath failed: ${String(err)}`,
      )
    }
    try {
      await materializeCommands(target)
      await materializeAgents(target)
    } catch (err) {
      console.warn(
        `[astrojones] materializeCommands/Agents failed: ${String(err)}`,
      )
    }
    try {
      await checkDrift(target)
    } catch {
      // drift is a warning; never block
    }
  })()
  return bootstrapped
}

export const AstrojonesDev: Plugin = async ({ worktree, directory }) => {
  const target = worktree ?? directory ?? process.cwd()

  void bootstrapOnce(target).catch(() => {
    /* fail-open: a bootstrap failure must not break opencode */
  })

  return {
    "tool.execute.before": async (input, output) => {
      await bootstrapOnce(target)
      if (input.tool !== "bash" && input.tool !== "Bash") return
      const command =
        typeof (output.args as { command?: unknown })?.command === "string"
          ? ((output.args as { command: string }).command)
          : ""
      if (!command) return
      const decision = await policyCheck(command, target)
      // Hard-deny only here: opencode's tool.execute.before is allow/deny, so a
      // throw is the only way to block. The harness "confirm-first" tier
      // (requires_confirmation) is NOT a deny — record the callID so the
      // permission.ask hook can prompt interactively instead of hard-blocking.
      if (!decision.allowed) {
        throw new Error(
          `[astrojones] command blocked by policy: ${decision.reason}`,
        )
      }
      if (decision.requires_confirmation) {
        confirmFirstCallIDs.add(input.callID)
      }
    },
    "permission.ask": async (input, output) => {
      // Restore the interactive confirm-first tier: if tool.execute.before
      // flagged this tool call as requires_confirmation, prompt the user
      // instead of auto-approving. Otherwise leave opencode's own decision
      // untouched. (See confirmFirstCallIDs for the firing-order assumption.)
      if (input.callID && confirmFirstCallIDs.delete(input.callID)) {
        output.status = "ask"
      }
    },
  }
}

// --- V2 plugin (OpenCode 2) -------------------------------------------------
// OpenCode 2 dropped the V1 plugin API: a V1 factory returning a hook map no
// longer loads ("Plugin must export a default definition with an id and an
// effect or setup function"). This half ports the same behavior onto the V2
// API. The types are declared structurally here instead of importing
// @opencode/plugin, so the file keeps loading with zero runtime dependencies
// under both V1 and V2 — the same dual-target approach the upstream
// superpowers plugin uses.
type V2ToolHookEvent = { readonly tool: string; readonly input: unknown }

type V2PermissionEvaluation = {
  readonly sessionID: string
  readonly agent?: string
  readonly action: string
  readonly resources: readonly string[]
  readonly metadata?: Record<string, unknown>
  effect: "allow" | "ask" | "deny"
  message?: string
}

type V2PluginContext = {
  readonly location?: {
    readonly directory?: string
    readonly project?: { readonly directory?: string; readonly canonical?: string }
  }
  readonly tool: {
    hook(
      name: "execute.before",
      callback: (event: V2ToolHookEvent) => void | Promise<void>,
    ): Promise<unknown>
  }
  readonly permission: {
    hook(
      name: "evaluate",
      callback: (event: V2PermissionEvaluation) => void | Promise<void>,
    ): Promise<unknown>
  }
}

async function setup(ctx: V2PluginContext): Promise<void> {
  // V1 also invokes a default export's setup() but with a V1-shaped context
  // that lacks the tool/permission domains. Detect that and return quietly;
  // V1 is served entirely by the AstrojonesDev named export.
  if (!ctx?.tool?.hook || !ctx?.permission?.hook) return

  const target =
    ctx.location?.directory ??
    ctx.location?.project?.canonical ??
    process.cwd()

  void bootstrapOnce(target).catch(() => {
    /* fail-open: a bootstrap failure must not break opencode */
  })

  // V2 evaluates permissions before a shell command runs, and this single hook
  // replaces BOTH V1 hooks: an evaluate hook may set effect to "deny" as well
  // as "ask". The V1 split existed only because tool.execute.before was
  // allow/deny and could not request confirmation; that limitation is gone, so
  // the harness policy check runs once per evaluation instead of twice.
  // `resources` carries the scanner-produced command string(s) for a shell
  // action, so no callID join between hooks is needed.
  try {
    await ctx.permission.hook("evaluate", async (event) => {
      if (event.action !== "shell") return
      await bootstrapOnce(target)
      let confirm = false
      for (const resource of event.resources) {
        const decision = await policyCheck(resource, target)
        if (!decision.allowed) {
          event.effect = "deny"
          event.message = `[astrojones] command blocked by policy: ${decision.reason}`
          return
        }
        if (decision.requires_confirmation) confirm = true
      }
      // Never downgrade an explicit deny; only escalate allow -> ask.
      if (confirm && event.effect === "allow") event.effect = "ask"
    })
  } catch (err) {
    console.warn(`[astrojones] V2 permission hook registration failed: ${String(err)}`)
  }

  // Belt-and-suspenders safety floor. The harness check above is the primary
  // gate; this cheap, subprocess-free regex fallback mirrors the V1 design
  // comment ("the safety floor never depends on permission.ask firing") so an
  // unloaded or erroring evaluate hook still blocks the obvious nukes.
  try {
    await ctx.tool.hook("execute.before", async (event) => {
      if (event.tool !== "bash" && event.tool !== "Bash" && event.tool !== "shell") return
      const command =
        typeof (event.input as { command?: unknown })?.command === "string"
          ? (event.input as { command: string }).command
          : ""
      if (!command) return
      for (const re of DESTRUCTIVE_FALLBACK) {
        if (re.test(command)) {
          throw new Error(
            `[astrojones] command blocked by built-in fallback (${re})`,
          )
        }
      }
    })
  } catch (err) {
    console.warn(`[astrojones] V2 tool hook registration failed: ${String(err)}`)
  }
}

// Default export: { id, server, setup }.
// V2 reads { id, setup }; V1 reads the named export / server() function.
export default {
  id: "astrojones",
  server: AstrojonesDev,
  setup,
}
