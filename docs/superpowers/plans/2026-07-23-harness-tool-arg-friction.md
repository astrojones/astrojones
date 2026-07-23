# Harness Tool-Argument Friction Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate the recurring MCP tool-call validation errors agents hit against the repo-agent-harness (wrong parameter names, and the flat-vs-`inp`-wrapper schema inconsistency) so natural first-guess calls succeed.

**Architecture:** Two mechanisms, matched to how each tool is served. (1) Flat FastMCP tools (`repo_*`) accept common alias names via Pydantic `AliasChoices` — published schema keeps the canonical name, input accepts either. (2) Verbatim-proxied Serena tools are normalized at the proxy boundary (`_ProxiedSerenaTool.run`) using a per-tool alias map gated by the tool's real inputSchema, so a rewrite can never inject an unknown field. (3) The `inp`-wrapped tools are the actual root cause of the highest-frequency errors — they are flattened to individual params (reconstructing the Pydantic model in-body) so their published schema matches every other tool.

**Tech Stack:** Python 3.13–3.14, FastMCP, Pydantic v2, pytest, ruff (preview), ty.

## Global Constraints

- Line length 120; google docstrings; `Any` banned (ANN401); relative imports banned. (ruff preview, per `servers/harness-mcp/pyproject.toml`.)
- Run every command from `servers/harness-mcp/` unless noted. Test with `uv run pytest`, lint `uv run ruff check`, types `uv run ty check`.
- Do NOT touch `serena_tools.json` — no Serena schema changes; we only normalize inputs. The pin stays fixed.
- Pre-existing `ty` errors in `gateway.py` (`_resolve_root`/`serena_args`/`ensure_daemon`, category `call-top-callable` + `invalid-*`) exist on HEAD and are OUT OF SCOPE. Do not "fix" them; only ensure no NEW ty error is introduced.
- Stage files explicitly by path. Conventional Commits. Never `git add -A`.

---

## Already landed (in the working tree, verified green — 80 scoped tests pass)

These are done; do not redo them. They are the baseline the tasks below extend.

- **`server.py`** — `repo_read_range`, `repo_search_text`, `repo_search_files` params carry `validation_alias=AliasChoices(...)`: `start`/`end`→`start_line`/`end_line`, `query`→`pattern` + `path`→`paths`, `glob`/`path`→`pattern`. Import is `from pydantic import AliasChoices, Field`.
- **`gateway.py`** — `_SERENA_ARG_ALIASES` + `_alias_map_for()` (gates aliases by the tool's own inputSchema) + `_normalize_arguments()`; `_ProxiedSerenaTool` gained `_aliases` (PrivateAttr) applied at the top of `run()`; `proxied_tools()` sets `tool._aliases = _alias_map_for(entry.get("inputSchema"))`. Current alias table: `name_path→name_path_pattern`, `path/file_path/file→relative_path`.
- **`tests/fake_serena.py`** — `find_symbol` accepts both `name_path` and `name_path_pattern` (coalesced) so direct-call tests and the normalizing `run()` path both work.
- **Tests** — `tests/test_gateway.py`: `test_alias_map_is_gated_by_tool_schema`, `test_normalize_arguments_rewrites_and_dedupes`, `test_proxied_tool_normalizes_alias_before_forwarding`. `tests/test_server.py`: `test_repo_tool_schemas_advertise_only_canonical_names`, `test_repo_tools_accept_param_aliases`.

---

## Task C: Serena reverse-alias + replace_content aliases

Covers two confirmed last-7-days errors: (1) agents who over-corrected `name_path`→`name_path_pattern` on tools that actually want `name_path` (`replace_symbol_body`, `insert_after_symbol`, `insert_before_symbol`, `find_referencing_symbols`, `rename_symbol`); (2) `serena_replace_content` called with `replacement`/`new_string` instead of `repl`.

**Files:**
- Modify: `servers/harness-mcp/repo_agent_harness/gateway.py` (the `_SERENA_ARG_ALIASES` dict only)
- Test: `servers/harness-mcp/tests/test_gateway.py`

**Interfaces:**
- Consumes: `gateway._alias_map_for(input_schema) -> dict[str,str]`, `gateway._normalize_arguments(args, aliases) -> dict` (already landed).
- Produces: no signature change — only the contents of `_SERENA_ARG_ALIASES` grow. Gating in `_alias_map_for` guarantees the two `name_path`⇄`name_path_pattern` directions never both activate for one tool (a tool declares exactly one of the pair).

- [ ] **Step 1: Write the failing tests**

Append to `servers/harness-mcp/tests/test_gateway.py` after `test_alias_map_is_gated_by_tool_schema`:

```python
def test_alias_map_reverse_name_path_for_edit_tools():
    # find_symbol declares name_path_pattern -> forward alias only
    fwd = gateway._alias_map_for({"properties": {"name_path_pattern": {}}})
    assert fwd.get("name_path") == "name_path_pattern"
    assert "name_path_pattern" not in fwd  # reverse inactive here
    # replace_symbol_body/insert_*/find_referencing_symbols/rename_symbol declare name_path -> reverse alias
    rev = gateway._alias_map_for({"properties": {"name_path": {}, "relative_path": {}, "body": {}}})
    assert rev.get("name_path_pattern") == "name_path"
    assert "name_path" not in rev  # forward inactive here


def test_alias_map_replace_content_repl():
    m = gateway._alias_map_for({"properties": {"relative_path": {}, "needle": {}, "repl": {}, "mode": {}}})
    assert m.get("replacement") == "repl"
    assert m.get("new_string") == "repl"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gateway.py::test_alias_map_reverse_name_path_for_edit_tools tests/test_gateway.py::test_alias_map_replace_content_repl -q`
Expected: FAIL (`name_path_pattern` / `replacement` keys absent from the map).

- [ ] **Step 3: Extend the alias table**

In `servers/harness-mcp/repo_agent_harness/gateway.py`, replace the `_SERENA_ARG_ALIASES` dict body with:

```python
_SERENA_ARG_ALIASES: dict[str, str] = {
    # find_symbol renamed name_path -> name_path_pattern; the edit/reference tools kept name_path.
    # Both directions live here; _alias_map_for activates only the one whose canonical the tool
    # declares, so a tool that has name_path_pattern gets the forward map and a tool that has
    # name_path gets the reverse — they can never both fire for one tool.
    "name_path": "name_path_pattern",
    "name_path_pattern": "name_path",
    "path": "relative_path",
    "file_path": "relative_path",
    "file": "relative_path",
    # replace_content's replacement field is `repl`; agents reach for edit-tool vocabulary.
    "replacement": "repl",
    "new_string": "repl",
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gateway.py -q`
Expected: PASS (all gateway tests, including the landed ones).

- [ ] **Step 5: Commit**

```bash
git add servers/harness-mcp/repo_agent_harness/gateway.py servers/harness-mcp/tests/test_gateway.py
git commit -m "fix(harness): reverse name_path alias for serena edit tools + replace_content repl alias"
```

---

## Task D: repo_read_range `relative_path` → `path` alias

Confirmed error: agents call `repo_read_range` with Serena-style `relative_path` instead of `path`.

**Files:**
- Modify: `servers/harness-mcp/repo_agent_harness/server.py` (the `repo_read_range` `path` param)
- Test: `servers/harness-mcp/tests/test_server.py`

**Interfaces:**
- Consumes: `AliasChoices` (already imported in `server.py`).
- Produces: `repo_read_range` published schema still shows only `path`/`start_line`/`end_line`; input additionally accepts `relative_path`.

- [ ] **Step 1: Write the failing test**

Append to `servers/harness-mcp/tests/test_server.py`, inside the param-aliases section:

```python
def test_repo_read_range_accepts_relative_path_alias(repo, monkeypatch):
    monkeypatch.chdir(repo)
    assert _run_tool("repo_read_range", {"relative_path": "pyproject.toml", "start_line": 1, "end_line": 2}) == _run_tool(
        "repo_read_range", {"path": "pyproject.toml", "start_line": 1, "end_line": 2}
    )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_repo_read_range_accepts_relative_path_alias -q`
Expected: FAIL — `relative_path` is an unexpected keyword.

- [ ] **Step 3: Add the alias**

In `servers/harness-mcp/repo_agent_harness/server.py`, change the `repo_read_range` `path` param from:

```python
    path: Annotated[str, Field(description="Repo-relative file path")],
```
to:
```python
    path: Annotated[
        str, Field(description="Repo-relative file path", validation_alias=AliasChoices("path", "relative_path"))
    ],
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_server.py::test_repo_read_range_accepts_relative_path_alias -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add servers/harness-mcp/repo_agent_harness/server.py servers/harness-mcp/tests/test_server.py
git commit -m "fix(harness): accept relative_path alias on repo_read_range"
```

---

## Task E: Flatten `repo_symbols_overview` (the 10×/5-session offender)

Root cause: `repo_symbols_overview(inp: symbols.SymbolsOverviewIn)` publishes a nested `inp` object; agents pass flat `path`/`relative_path`. Flatten the signature; reconstruct the model in-body (the model stays the SSOT for defaults/validation).

**Files:**
- Modify: `servers/harness-mcp/repo_agent_harness/server.py:433-442` (`repo_symbols_overview`)
- Test: `servers/harness-mcp/tests/test_server.py`

**Interfaces:**
- Consumes: `symbols.SymbolsOverviewIn(path: str | None = None, limit: int = 200)` (unchanged), `symbols.overview(root, inp)` (unchanged).
- Produces: `repo_symbols_overview` published schema exposes `path` and `limit` as top-level properties; `inp` no longer appears.

- [ ] **Step 1: Check for direct callers (must stay working)**

Run: `uv run python -c "import subprocess; print(subprocess.run(['git','grep','-n','repo_symbols_overview(','--','servers/harness-mcp'],capture_output=True,text=True).stdout)"`
Expected: only the definition and MCP registration; if any code calls it with `inp=`, that call site must switch to `path=`/`limit=` in this task. (CLI uses `symbols.overview` directly, not the tool.)

- [ ] **Step 2: Write the failing tests**

Append to `servers/harness-mcp/tests/test_server.py` param-aliases section:

```python
def test_symbols_overview_schema_is_flat():
    import asyncio

    async def go():
        return await server.mcp.get_tool("repo_symbols_overview")
    tool = asyncio.run(go())
    props = set(tool.parameters.get("properties", {}))
    assert "inp" not in props
    assert {"path", "limit"} <= props


def test_symbols_overview_accepts_flat_params(repo, monkeypatch):
    monkeypatch.chdir(repo)
    out = _run_tool("repo_symbols_overview", {"path": "src", "limit": 50})
    assert "symbols" in out and "error" not in out
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_server.py::test_symbols_overview_schema_is_flat tests/test_server.py::test_symbols_overview_accepts_flat_params -q`
Expected: FAIL — schema exposes `inp`, flat call errors with `inp Missing required argument`.

- [ ] **Step 4: Flatten the signature**

Replace `repo_symbols_overview` in `servers/harness-mcp/repo_agent_harness/server.py` with:

```python
@mcp.tool()
def repo_symbols_overview(
    path: Annotated[
        str | None, Field(description="Repo-relative file or directory prefix to scope to; None = whole repo")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=2000, description="Maximum symbols to return")] = 200,
) -> dict:
    """Symbol map from the static tree-sitter index — no Serena launch, always current.

    The preferred first move for code orientation: names, kinds, spans, and nesting per
    file (optionally scoped to a path prefix). Reserve serena_* for the semantic questions
    the index cannot answer — references, implementations, diagnostics, renames.
    """
    root = git.repo_root()
    if not root:
        return _no_repo()
    inp = symbols.SymbolsOverviewIn(path=path, limit=limit)
    return symbols.overview(root, inp).model_dump(exclude_none=True)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add servers/harness-mcp/repo_agent_harness/server.py servers/harness-mcp/tests/test_server.py
git commit -m "fix(harness): flatten repo_symbols_overview params (drop inp wrapper)"
```

---

## Task F: Flatten the `mem_*` tools for schema consistency

Same root cause as Task E, applied to `mem_search`, `mem_remember`, `mem_ingest`, `mem_stats`, `mem_ontology` (all take a single `inp` model). Lower call frequency, but leaving them wrapped keeps the inconsistency that makes agents guess wrong in both directions. Each transform is mechanical: replace the `inp: models.XIn` param with the model's fields, then build `inp = models.XIn(...)` as the first body line, keeping the rest of the body verbatim.

**Files:**
- Modify: `servers/harness-mcp/repo_agent_harness/server.py` (the five `mem_*` tool defs, ~lines 700–745)
- Modify: `servers/harness-mcp/repo_agent_harness/server.py` import line — add `Literal` (needed by `mem_search`): `from typing import TYPE_CHECKING, Annotated, Literal, override`
- Test: `servers/harness-mcp/tests/test_server.py`

**Interfaces (field maps — copy verbatim from `models.py`):**
- `MemSearchIn`: `query: str`, `search_type: Literal["GRAPH_COMPLETION","CHUNKS","TEMPORAL","CODING_RULES"]="GRAPH_COMPLETION"`, `dataset: str|None=None`, `node_name: list[str]|None=None`, `top_k: int (ge=1,le=50)=10`.
- `MemRememberIn`: `text: str`, `dataset: str|None=None`, `node_set: list[str]|None=None`, `metadata: dict|None=None`.
- `MemIngestIn`: `items: list[str]`, `dataset: str|None=None`, `node_set: list[str]|None=None`, `ontology_key: str|None=None`, `dry_run: bool=False`, `confirm: bool=False`.
- `MemStatsIn`: `dataset: str`.
- `MemOntologyIn`: `individuals: dict[str, str]`.

- [ ] **Step 1: Read the five current tool bodies**

Run: `uv run repo-agent-harness-mcp --help >/dev/null 2>&1; sed -n '698,748p' repo_agent_harness/server.py` *(read-only; capture each body so it can be preserved verbatim below the reconstructed `inp` line — do NOT change behaviour, only the signature + first line).*

- [ ] **Step 2: Write the failing test (worked example: `mem_search`)**

Append to `servers/harness-mcp/tests/test_server.py`:

```python
def test_mem_tools_schemas_are_flat():
    import asyncio

    async def props(name: str) -> set[str]:
        return set((await server.mcp.get_tool(name)).parameters.get("properties", {}))

    assert "inp" not in asyncio.run(props("mem_search"))
    assert {"query", "search_type", "dataset", "top_k"} <= asyncio.run(props("mem_search"))
    for name in ("mem_remember", "mem_ingest", "mem_stats", "mem_ontology"):
        assert "inp" not in asyncio.run(props(name)), name
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_server.py::test_mem_tools_schemas_are_flat -q`
Expected: FAIL — every `mem_*` schema exposes `inp`.

- [ ] **Step 4: Flatten `mem_search` (worked example)**

In `server.py`, change the `mem_search` definition from `def mem_search(inp: models.MemSearchIn) -> dict:` to the flat signature, and add the reconstruction as the first body statement, preserving the existing remainder verbatim:

```python
@mcp.tool()
def mem_search(
    query: Annotated[str, Field(description="Natural-language query against the memory graph")],
    search_type: Annotated[
        Literal["GRAPH_COMPLETION", "CHUNKS", "TEMPORAL", "CODING_RULES"], Field(description="Retrieval mode")
    ] = "GRAPH_COMPLETION",
    dataset: Annotated[str | None, Field(description="Dataset name; None = the user's default scope")] = None,
    node_name: Annotated[list[str] | None, Field(description="Restrict to these node_set tags")] = None,
    top_k: Annotated[int, Field(ge=1, le=50)] = 10,
) -> dict:
    """<keep the existing mem_search docstring verbatim>"""
    inp = models.MemSearchIn(query=query, search_type=search_type, dataset=dataset, node_name=node_name, top_k=top_k)
    # <keep the existing body from here down verbatim — it already consumes `inp`>
```

- [ ] **Step 5: Flatten `mem_remember`, `mem_ingest`, `mem_stats`, `mem_ontology`**

Apply the identical transform to each, using the field maps in the Interfaces block. Pattern per tool: flat `Annotated[...] ` params (descriptions copied from `models.py`), then `inp = models.<XIn>(<field>=<param>, ...)` as the first body line, rest verbatim. Add `Literal` to the `typing` import (Step done once).

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_server.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add servers/harness-mcp/repo_agent_harness/server.py servers/harness-mcp/tests/test_server.py
git commit -m "fix(harness): flatten mem_* tool params (drop inp wrapper) for schema consistency"
```

---

## Task G: Full verification

**Files:** none (verification only).

- [ ] **Step 1: Lint all changed files**

Run: `uv run ruff check repo_agent_harness/ tests/`
Expected: `All checks passed!`

- [ ] **Step 2: Type-check changed source, confirm no NEW errors**

Run: `uv run ty check repo_agent_harness/server.py repo_agent_harness/gateway.py`
Expected: `server.py` clean; `gateway.py` shows ONLY the pre-existing `_resolve_root`/`serena_args`/`ensure_daemon` errors listed in Global Constraints — no new error at the alias code.

- [ ] **Step 3: Full test suite**

Run: `uv run pytest -q`
Expected: all pass (the run was previously interrupted; confirm green end-to-end).

- [ ] **Step 4: Sanity-check the published schemas end-to-end**

Run:
```bash
uv run python -c "
import asyncio
from repo_agent_harness import server as S
async def go():
    for n in ['repo_read_range','repo_search_text','repo_search_files','repo_symbols_overview','mem_search']:
        t = await S.mcp.get_tool(n)
        print(n, sorted(t.parameters.get('properties',{})))
asyncio.run(go())
"
```
Expected: every tool lists flat, canonical property names; none lists `inp`.

- [ ] **Step 5: Final commit if anything pending**

```bash
git status
git add -p  # only harness files
git commit -m "test(harness): verify tool-arg friction fixes end-to-end"
```

---

## Self-Review

- **Spec coverage:** The 7-day scan's items map to tasks — `name_path`/`name_path_pattern` split (landed + Task C reverse), `repo_read_range start/end` (landed) + `relative_path` (Task D), `repo_search_text query/path/glob` (landed), `repo_search_files glob/path` (landed), `serena_replace_content repl/mode` (Task C — `mode` is a required literal the agent must still supply; only the `repl` name is aliased), `repo_symbols_overview inp` (Task E), `mem_search inp` + sibling `mem_*` (Task F). Shape/type errors (`paths` as string, malformed JSON) and infra errors (Connection closed, tool-not-registered) are explicitly OUT OF SCOPE — they are agent/environment faults, not schema friction; noted here so they are not silently assumed covered.
- **Placeholder scan:** `mem_*` bodies in Task F are preserved verbatim (Step 1 reads them); the transform is shown fully for `mem_search` and specified field-by-field for the rest — mechanical and identical, not a placeholder.
- **Type consistency:** `_alias_map_for`/`_normalize_arguments` signatures unchanged; model constructors use the exact field names from `models.py`/`symbols.py` verified in this session.
