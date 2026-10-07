# astrojones — Antigravity Plugin

The Google Antigravity integration of the `astrojones` repo agent harness.
It brings safe, deterministic repo tooling, safety hooks, workflow skills, subagents, and single-instance Serena code navigation to Google Antigravity.

---

## What it provides

1. **Bundled MCP Servers (`mcp_config.json`)**:
   - **`repo-agent-harness`**: Deterministic repo facts and navigation (`repo_symbols_overview`, `repo_context_overview`, `repo_search_text`, `repo_read_range`, `repo_impact_file`, `repo_verify_changed`, etc.).
   - **`serena`**: Proxies read-only semantic LSP tools (`find_symbol`, `find_referencing_symbols`, `get_symbols_overview`, etc.) via `serena-shim` connecting to the single Serena daemon (`127.0.0.1:24225`) on your MacBook, preventing duplicate instances.

2. **Lifecycle Hooks (`hooks.json`)**:
   - **`PreToolUse`**: Enforces repo policies against dangerous commands (`rm -rf /`, force push, etc.) and blocks reading secret files (`.env`, credentials, PEM files).
   - **`PostToolUse`**: Records touched files and coordinates verification nudges.

3. **Subagents (`agents/`)**:
   - **`explorer`**: Read-only symbol-level code location (replaces generic exploration with static tree-sitter index + Serena).
   - **`architect`**: Read-only architecture design and step planning with disjoint implementation streams.
   - **`implementer`**: Strict TDD implementation (RED/GREEN/REFACTOR) scoped to assigned files.
   - **`reviewer`**: Read-only pre-commit diff review (correctness, scope creep, secret leaks).
   - **`test-runner`**: Targeted verification and failure triage.

4. **Workflow Skills (`skills/`)**:
   - `bugfix`, `feature`, `implement`, `plan`, `refactor`, `test`, `commit`.

5. **Rules (`rules/AGENTS.md`)**:
   - Enforces symbol navigation and range reads instead of dumping whole files.

---

## Installation / Setup

### 1. Global Installation (Machine-wide)

Symlink the plugin into your Antigravity global plugins directory:

```bash
mkdir -p ~/.gemini/config/plugins
ln -s /Users/jonah/dev/astrojones ~/.gemini/config/plugins/astrojones
```

*(Alternatively, symlink `astrojones/antigravity`)*

Once symlinked, Antigravity automatically discovers and activates `astrojones` across all projects.

### 2. Project-level Materialization (Optional)

In any project workspace, run:

```bash
repo-agent-harness bootstrap --target antigravity
```

This materializes `.agents/mcp_config.json` in the target repository. Use `--target all` to materialize for Claude Code, OpenCode, and Antigravity simultaneously.
