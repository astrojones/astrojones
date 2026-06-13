import { describe, it, expect } from "vitest"
import {
  DESTRUCTIVE_FALLBACK,
  claudeToOpencode,
  firstLine,
  mapSkillsPaths,
  SKILLS_PATH_SENTINELS,
} from "./astrojones-dev"

function matchesDestructive(cmd: string): boolean {
  return DESTRUCTIVE_FALLBACK.some((re) => re.test(cmd))
}

describe("DESTRUCTIVE_FALLBACK", () => {
  const positives: Array<[string, string]> = [
    ["rm -rf /", "combined recursive+force short flag"],
    ["rm -fr /", "combined force+recursive short flag"],
    ["rm -r -f /", "separated short flags"],
    ["rm -f -r /", "separated short flags reversed"],
    ["rm --recursive --force /etc", "long flags"],
    ["rm --force --recursive /etc", "long flags reversed"],
    ["sudo rm -r -f /var", "with leading sudo"],
    ["git push --force origin main", "git force push long"],
    ["git push -f", "git force push short"],
    ["gh repo delete owner/x", "gh repo delete"],
    ["chmod -R 777 /srv", "recursive chmod 777"],
    ["docker compose down -v", "docker down with volumes"],
  ]
  it.each(positives)("blocks %s (%s)", (cmd) => {
    expect(matchesDestructive(cmd)).toBe(true)
  })

  const negatives: Array<[string, string]> = [
    ["rm file.txt", "plain rm single file"],
    ["rm -r dir", "recursive only, no force"],
    ["rm -f file", "force only, no recursive"],
    ["npm install", "unrelated command"],
    ["git push origin main", "non-force push"],
    ["ls -la", "listing"],
    ["docker compose down", "down without volumes"],
  ]
  it.each(negatives)("allows %s (%s)", (cmd) => {
    expect(matchesDestructive(cmd)).toBe(false)
  })
})

describe("firstLine", () => {
  it("returns text before first newline", () => {
    expect(firstLine("hello\nworld")).toBe("hello")
  })
  it("returns whole string when no newline", () => {
    expect(firstLine("solo")).toBe("solo")
  })
})

describe("claudeToOpencode", () => {
  it("returns body unchanged when no frontmatter", () => {
    expect(claudeToOpencode("no frontmatter here")).toBe("no frontmatter here")
  })

  it("strips inline-style claude-only keys, keeps others", () => {
    const input = [
      "---",
      "name: thing",
      "color: blue",
      "allowed-tools: Read, Edit",
      "argument-hint: <x>",
      "description: a thing",
      "---",
      "body text",
    ].join("\n")
    const out = claudeToOpencode(input)
    expect(out).toContain("name: thing")
    expect(out).toContain("description: a thing")
    expect(out).not.toContain("color:")
    expect(out).not.toContain("allowed-tools:")
    expect(out).not.toContain("argument-hint:")
    expect(out).toContain("body text")
  })

  it("drops block-style key continuation lines (M1)", () => {
    const input = [
      "---",
      "name: thing",
      "allowed-tools:",
      "  - Read",
      "  - Edit",
      "description: a thing",
      "---",
      "body",
    ].join("\n")
    const out = claudeToOpencode(input)
    expect(out).not.toContain("allowed-tools:")
    expect(out).not.toContain("- Read")
    expect(out).not.toContain("- Edit")
    expect(out).toContain("name: thing")
    expect(out).toContain("description: a thing")
  })
})

describe("mapSkillsPaths (sentinel rewrite)", () => {
  const resolved = "/abs/skills"
  it.each(SKILLS_PATH_SENTINELS)("rewrites sentinel %s to resolved path", (sentinel) => {
    expect(mapSkillsPaths([sentinel], resolved)).toEqual([resolved])
  })
  it("preserves non-sentinel paths", () => {
    expect(mapSkillsPaths(["./local", "/other/path"], resolved)).toEqual([
      "./local",
      "/other/path",
    ])
  })
  it("rewrites only sentinels in a mixed list", () => {
    expect(
      mapSkillsPaths(["<set-by-opencode-plugin>", "./keep"], resolved),
    ).toEqual([resolved, "./keep"])
  })
})
