# Active Harness — Walking Skeleton Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Prove the core inversion end-to-end — an agent makes an AST-node edit, the harness reactively runs a configured check on its own, and the result reaches the agent as a piggybacked event on a *later, unrelated* tool response, with no verification tool ever called.

**Architecture:** A standalone Python MCP server (fastmcp). One target language (Python) is navigated/edited via a lossless tree-sitter byte-splice. A reactive engine runs configured checks (ruff) after each edit and enqueues typed events. A cross-cutting envelope drains that queue onto *every* tool response (the piggyback channel). A declarative "harness program" (TOML) declares the language + checks; a generator writes that program and a machine-managed AGENTS.md section.

**Tech Stack:** Python 3.13+, `uv`, `fastmcp` + `mcp`, `tree-sitter` (<0.26) + `tree-sitter-language-pack`, `ruff` (target-language linter *and* the project's own linter), `ty` (typecheck), `pytest`.

## Global Constraints

- Python 3.13+ ; `tree-sitter` pinned `<0.26` (0.26 ABI-breaks the wheels), verbatim.
- Ruff preview rules ; google docstrings ; line length 120 ; relative imports banned ; `Any` banned (ANN401) ; bandit (S) on. Generate `pyproject.toml` via the `pyproject-canon` skill — do not hand-roll.
- Type checker is `ty` (not mypy) ; `unresolved-import`/`unresolved-reference`/`invalid-assignment` = error.
- Strict TDD: every task is RED → GREEN → commit. No production code before a failing test.
- Stage files explicitly by path in every commit — never `git add -A`.
- Repo root: `~/dev/active-harness`. Package: `active_harness` under `src/`.

---

## File Structure

- `pyproject.toml` — project + tool config (via pyproject-canon).
- `src/active_harness/__init__.py` — package marker + version.
- `src/active_harness/events.py` — `Event` model + `EventQueue` (session-scoped enqueue/drain). One responsibility: event state.
- `src/active_harness/ast_edit.py` — tree-sitter load + `replace_node_body` (lossless byte-splice) + `overview` (top-level symbol list). One responsibility: the AST interface.
- `src/active_harness/config.py` — load the harness program (declared language + checks) from `harness.toml`.
- `src/active_harness/engine.py` — reactive engine: run a configured check on a changed file, map results to `Event`s. One responsibility: reaction.
- `src/active_harness/onboard.py` — generate `harness.toml` + the machine-managed AGENTS.md section. One responsibility: programming the harness.
- `src/active_harness/server.py` — fastmcp server: registers `ast_overview` (read) + `ast_replace_node_body` (edit), wires engine + queue, wraps every response in the envelope.
- `tests/…` — one test module per source module.

---

### Task 1: Project scaffold

**Files:**
- Create: `~/dev/active-harness/pyproject.toml`
- Create: `src/active_harness/__init__.py`
- Create: `tests/test_smoke.py`

**Interfaces:**
- Produces: an importable `active_harness` package with `__version__: str`.

- [ ] **Step 1: Init repo + git**

```bash
mkdir -p ~/dev/active-harness && git -C ~/dev/active-harness init
```

- [ ] **Step 2: Generate pyproject via the canonical skill**

Invoke the `pyproject-canon` skill for a new package named `active-harness` (src layout, Python 3.13+, deps: `fastmcp`, `mcp`, `tree-sitter<0.26`, `tree-sitter-language-pack`; dev group: `pytest`, `ruff`, `ty`). Do not write the file by hand.

- [ ] **Step 3: Write the smoke test (RED)**

```python
# tests/test_smoke.py
def test_package_imports():
    import active_harness

    assert isinstance(active_harness.__version__, str)
```

- [ ] **Step 4: Run it — expect failure**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_smoke.py -v`
Expected: FAIL (`ModuleNotFoundError` / missing `__version__`).

- [ ] **Step 5: Minimal package**

```python
# src/active_harness/__init__.py
"""Active Harness — AST-interface, reactive-tooling MCP server."""

__version__ = "0.0.1"
```

- [ ] **Step 6: Green + lint + typecheck**

Run: `uv run --directory ~/dev/active-harness pytest -q && uv run --directory ~/dev/active-harness ruff check && uv run --directory ~/dev/active-harness ty check`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git -C ~/dev/active-harness add pyproject.toml src/active_harness/__init__.py tests/test_smoke.py
git -C ~/dev/active-harness commit -m "chore: scaffold active-harness package"
```

---

### Task 2: Event model + session queue

**Files:**
- Create: `src/active_harness/events.py`
- Test: `tests/test_events.py`

**Interfaces:**
- Produces:
  - `Event(kind: str, severity: str, summary: str, source: str)` — a pydantic model. `severity ∈ {"error","warn","info"}`.
  - `EventQueue` with `enqueue(event: Event) -> None`, `drain() -> list[Event]` (returns all pending and clears), `pending() -> int`.

- [ ] **Step 1: Failing test (RED)**

```python
# tests/test_events.py
from active_harness.events import Event, EventQueue


def test_enqueue_then_drain_clears():
    q = EventQueue()
    q.enqueue(Event(kind="lint", severity="error", summary="2 issues", source="ruff"))
    assert q.pending() == 1
    drained = q.drain()
    assert len(drained) == 1
    assert drained[0].summary == "2 issues"
    assert q.pending() == 0  # drain clears
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_events.py -v`
Expected: FAIL (`ModuleNotFoundError: active_harness.events`).

- [ ] **Step 3: Implement**

```python
# src/active_harness/events.py
"""Typed harness events and the session-scoped queue that carries them."""

from pydantic import BaseModel


class Event(BaseModel):
    """A single tooling result destined for the agent via the piggyback envelope."""

    kind: str
    severity: str
    summary: str
    source: str


class EventQueue:
    """In-memory, session-scoped FIFO of pending events."""

    def __init__(self) -> None:
        self._items: list[Event] = []

    def enqueue(self, event: Event) -> None:
        """Append one event to the pending set."""
        self._items.append(event)

    def pending(self) -> int:
        """Number of events waiting to be drained."""
        return len(self._items)

    def drain(self) -> list[Event]:
        """Return all pending events and clear the queue."""
        items, self._items = self._items, []
        return items
```

- [ ] **Step 4: Green**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_events.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C ~/dev/active-harness add src/active_harness/events.py tests/test_events.py
git -C ~/dev/active-harness commit -m "feat: event model and session-scoped queue"
```

---

### Task 3: AST edit — lossless `replace_node_body` + `overview`

**Files:**
- Create: `src/active_harness/ast_edit.py`
- Test: `tests/test_ast_edit.py`

**Interfaces:**
- Produces:
  - `overview(source: str) -> list[str]` — names of top-level `function_definition` / `class_definition` nodes, in source order.
  - `replace_node_body(source: str, symbol: str, new_body: str) -> str` — replace the `body` block of the named top-level function with `new_body` (caller supplies correctly-indented text incl. leading newline), splicing only that byte range. Raises `KeyError` if `symbol` not found.

- [ ] **Step 1: Failing tests (RED)**

```python
# tests/test_ast_edit.py
import pytest

from active_harness.ast_edit import overview, replace_node_body

SRC = 'def greet(name):\n    return "hi " + name\n\n\ndef other():\n    return 1\n'


def test_overview_lists_top_level_symbols():
    assert overview(SRC) == ["greet", "other"]


def test_replace_body_changes_only_target_and_is_lossless_elsewhere():
    out = replace_node_body(SRC, "greet", '\n    return "hello " + name')
    assert '"hello " + name' in out
    # 'other' is byte-for-byte untouched
    assert "def other():\n    return 1\n" in out
    # still parses to the same symbol set
    assert overview(out) == ["greet", "other"]


def test_missing_symbol_raises():
    with pytest.raises(KeyError):
        replace_node_body(SRC, "nope", "\n    pass")
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_ast_edit.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/active_harness/ast_edit.py
"""The AST interface: navigate and edit Python source as tree-sitter nodes."""

from tree_sitter import Node
from tree_sitter_language_pack import get_parser

_PARSER = get_parser("python")
_DEF_KINDS = {"function_definition", "class_definition"}


def _top_level_defs(source: str) -> list[Node]:
    """Top-level function/class definition nodes, in source order."""
    root = _PARSER.parse(source.encode()).root_node
    return [c for c in root.children if c.type in _DEF_KINDS]


def _name_of(node: Node) -> str:
    """The declared name of a definition node."""
    name = node.child_by_field_name("name")
    if name is None:  # defensive: grammar guarantees a name field
        raise KeyError("definition node has no name")
    return name.text.decode()


def overview(source: str) -> list[str]:
    """Names of top-level definitions, in source order."""
    return [_name_of(n) for n in _top_level_defs(source)]


def replace_node_body(source: str, symbol: str, new_body: str) -> str:
    """Splice ``new_body`` into the body block of the named top-level function."""
    data = source.encode()
    for node in _top_level_defs(source):
        if _name_of(node) != symbol:
            continue
        body = node.child_by_field_name("body")
        if body is None:  # defensive
            raise KeyError(f"{symbol!r} has no body")
        return (data[: body.start_byte] + new_body.encode() + data[body.end_byte :]).decode()
    raise KeyError(symbol)
```

- [ ] **Step 4: Green**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_ast_edit.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C ~/dev/active-harness add src/active_harness/ast_edit.py tests/test_ast_edit.py
git -C ~/dev/active-harness commit -m "feat: lossless tree-sitter node overview and body replace"
```

---

### Task 4: Config loader — the harness program

**Files:**
- Create: `src/active_harness/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces:
  - `Check(name: str, command: list[str])` — one configured check; `command` may contain the token `"{file}"`, substituted with the changed file path at run time.
  - `HarnessProgram(language: str, checks: list[Check])`.
  - `load_program(path: Path) -> HarnessProgram` — parse a `harness.toml`.

- [ ] **Step 1: Failing test (RED)**

```python
# tests/test_config.py
from pathlib import Path

from active_harness.config import load_program

TOML = """
language = "python"

[[check]]
name = "ruff"
command = ["ruff", "check", "--output-format=json", "{file}"]
"""


def test_loads_language_and_checks(tmp_path: Path):
    p = tmp_path / "harness.toml"
    p.write_text(TOML)
    prog = load_program(p)
    assert prog.language == "python"
    assert prog.checks[0].name == "ruff"
    assert prog.checks[0].command[0] == "ruff"
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_config.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/active_harness/config.py
"""Load the declarative 'harness program' that the agent writes at onboard."""

import tomllib
from pathlib import Path

from pydantic import BaseModel


class Check(BaseModel):
    """One configured tool invocation; ``{file}`` is substituted at run time."""

    name: str
    command: list[str]


class HarnessProgram(BaseModel):
    """The active languages and checks the harness runs autonomously."""

    language: str
    checks: list[Check]


def load_program(path: Path) -> HarnessProgram:
    """Parse a ``harness.toml`` into a :class:`HarnessProgram`."""
    raw = tomllib.loads(path.read_text())
    checks = [Check(**c) for c in raw.get("check", [])]
    return HarnessProgram(language=raw["language"], checks=checks)
```

- [ ] **Step 4: Green**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_config.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git -C ~/dev/active-harness add src/active_harness/config.py tests/test_config.py
git -C ~/dev/active-harness commit -m "feat: harness-program config loader"
```

---

### Task 5: Reactive engine — run a check, emit events

**Files:**
- Create: `src/active_harness/engine.py`
- Test: `tests/test_engine.py`

**Interfaces:**
- Consumes: `HarnessProgram`, `Check` (Task 4); `Event`, `EventQueue` (Task 2).
- Produces:
  - `react(program: HarnessProgram, file_path: Path, queue: EventQueue) -> None` — run every check whose command references `{file}`, substituting `file_path`; for any check that exits non-zero, enqueue one `Event(kind="lint", severity="error", summary=..., source=check.name)`. Clean runs enqueue nothing.

- [ ] **Step 1: Failing test (RED) — uses real ruff on a temp file**

```python
# tests/test_engine.py
from pathlib import Path

from active_harness.config import Check, HarnessProgram
from active_harness.engine import react
from active_harness.events import EventQueue

PROGRAM = HarnessProgram(
    language="python",
    checks=[Check(name="ruff", command=["ruff", "check", "--output-format=json", "{file}"])],
)


def test_dirty_file_emits_error_event(tmp_path: Path):
    f = tmp_path / "bad.py"
    f.write_text("def f():\n    l = 1\n    return l\n")  # E741 ambiguous name 'l'
    q = EventQueue()
    react(PROGRAM, f, q)
    events = q.drain()
    assert len(events) == 1
    assert events[0].severity == "error"
    assert events[0].source == "ruff"


def test_clean_file_emits_nothing(tmp_path: Path):
    f = tmp_path / "ok.py"
    f.write_text("def f():\n    value = 1\n    return value\n")
    q = EventQueue()
    react(PROGRAM, f, q)
    assert q.pending() == 0
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_engine.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/active_harness/engine.py
"""The reactive engine: run configured checks after an edit and emit events."""

import subprocess  # noqa: S404 — running the repo's own configured toolchain
from pathlib import Path

from active_harness.config import HarnessProgram
from active_harness.events import Event, EventQueue


def react(program: HarnessProgram, file_path: Path, queue: EventQueue) -> None:
    """Run each file-scoped check; enqueue an error event per failing check."""
    for check in program.checks:
        if "{file}" not in check.command:
            continue
        cmd = [tok.replace("{file}", str(file_path)) for tok in check.command]
        proc = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603
        if proc.returncode != 0:
            queue.enqueue(
                Event(
                    kind="lint",
                    severity="error",
                    summary=f"{check.name}: issues in {file_path.name}",
                    source=check.name,
                )
            )
```

- [ ] **Step 4: Green**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_engine.py -v`
Expected: PASS (both cases).

- [ ] **Step 5: Commit**

```bash
git -C ~/dev/active-harness add src/active_harness/engine.py tests/test_engine.py
git -C ~/dev/active-harness commit -m "feat: reactive engine runs checks and emits events"
```

---

### Task 6: MCP server + piggyback envelope (the end-to-end proof)

**Files:**
- Create: `src/active_harness/server.py`
- Test: `tests/test_server.py`

**Interfaces:**
- Consumes: everything from Tasks 2–5.
- Produces:
  - `envelope(result: dict, queue: EventQueue) -> dict` — returns `{"result": result, "events": [event.model_dump() ...]}`, draining `queue`.
  - `Harness` — holds the loaded `HarnessProgram`, a workspace root, and one `EventQueue`. Methods (the two MCP tools, tested directly, not over stdio):
    - `ast_overview(rel_path: str) -> dict` — read tool; returns enveloped `{"symbols": [...]}`. Runs **no** checks.
    - `ast_replace_node_body(rel_path: str, symbol: str, new_body: str) -> dict` — writes the spliced source back to disk, then calls `react(...)`, then returns the enveloped edit result.

- [ ] **Step 1: Failing test (RED) — proves piggyback decoupling**

```python
# tests/test_server.py
from pathlib import Path

from active_harness.config import Check, HarnessProgram
from active_harness.server import Harness

PROGRAM = HarnessProgram(
    language="python",
    checks=[Check(name="ruff", command=["ruff", "check", "--output-format=json", "{file}"])],
)


def _harness(tmp_path: Path) -> Harness:
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n")
    return Harness(root=tmp_path, program=PROGRAM)


def test_edit_reacts_and_event_arrives_on_next_read(tmp_path: Path):
    h = _harness(tmp_path)
    # 1. edit introduces a lint error (E741) in the body of f
    edit = h.ast_replace_node_body("mod.py", "f", "\n    l = 1\n    return l")
    assert edit["result"]["ok"] is True
    # 2. a LATER, unrelated read tool call carries the reactive event — piggyback
    read = h.ast_overview("mod.py")
    assert read["result"]["symbols"] == ["f"]
    assert any(e["source"] == "ruff" and e["severity"] == "error" for e in read["events"])


def test_read_alone_runs_no_checks(tmp_path: Path):
    h = _harness(tmp_path)
    read = h.ast_overview("mod.py")
    assert read["events"] == []  # a read never triggers reaction
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_server.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/active_harness/server.py
"""The MCP server: AST tools wired to the reactive engine and piggyback envelope."""

from pathlib import Path

from active_harness.ast_edit import overview, replace_node_body
from active_harness.config import HarnessProgram
from active_harness.engine import react
from active_harness.events import EventQueue


def envelope(result: dict, queue: EventQueue) -> dict:
    """Wrap a tool result and attach (draining) all pending events."""
    return {"result": result, "events": [e.model_dump() for e in queue.drain()]}


class Harness:
    """One workspace session: program, root, event queue, and the AST tools."""

    def __init__(self, root: Path, program: HarnessProgram) -> None:
        self._root = root
        self._program = program
        self._queue = EventQueue()

    def ast_overview(self, rel_path: str) -> dict:
        """Read tool: list top-level symbols. Runs no checks."""
        source = (self._root / rel_path).read_text()
        return envelope({"symbols": overview(source)}, self._queue)

    def ast_replace_node_body(self, rel_path: str, symbol: str, new_body: str) -> dict:
        """Edit tool: splice a body, persist, then react and enqueue events."""
        path = self._root / rel_path
        path.write_text(replace_node_body(path.read_text(), symbol, new_body))
        react(self._program, path, self._queue)
        return envelope({"ok": True, "symbol": symbol}, self._queue)
```

- [ ] **Step 4: Green — the core chain is proven**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_server.py -v`
Expected: PASS. The edit's own response envelope may be empty of events depending on drain timing; the *decoupling* is proven by the event surfacing on the subsequent `ast_overview` read.

- [ ] **Step 5: Full suite + lint + typecheck**

Run: `uv run --directory ~/dev/active-harness pytest -q && uv run --directory ~/dev/active-harness ruff check && uv run --directory ~/dev/active-harness ty check`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git -C ~/dev/active-harness add src/active_harness/server.py tests/test_server.py
git -C ~/dev/active-harness commit -m "feat: MCP AST tools with reactive piggyback envelope"
```

---

### Task 7: Onboard generator — `harness.toml` + machine-managed AGENTS.md

**Files:**
- Create: `src/active_harness/onboard.py`
- Test: `tests/test_onboard.py`

**Interfaces:**
- Consumes: `HarnessProgram`, `Check` (Task 4).
- Produces:
  - `write_program(root: Path, program: HarnessProgram) -> Path` — serialize a `HarnessProgram` to `root/harness.toml`; returns the path.
  - `AGENTS_SECTION_START` / `AGENTS_SECTION_END` — sentinel markers.
  - `render_agents_section(program: HarnessProgram) -> str` — the machine-managed block instructing agents to (a) navigate/edit code only via AST tools, (b) read events instead of calling verification tools, (c) respect the autonomy policy, (d) drive process via superpowers.
  - `upsert_agents_md(root: Path, program: HarnessProgram) -> Path` — create or replace the marked section in `root/AGENTS.md`, preserving any human-authored text outside the markers.

- [ ] **Step 1: Failing tests (RED)**

```python
# tests/test_onboard.py
from pathlib import Path

from active_harness.config import Check, HarnessProgram
from active_harness.onboard import (
    AGENTS_SECTION_END,
    AGENTS_SECTION_START,
    upsert_agents_md,
    write_program,
)

PROGRAM = HarnessProgram(
    language="python",
    checks=[Check(name="ruff", command=["ruff", "check", "{file}"])],
)


def test_write_program_roundtrips(tmp_path: Path):
    from active_harness.config import load_program

    p = write_program(tmp_path, PROGRAM)
    assert p == tmp_path / "harness.toml"
    assert load_program(p).language == "python"


def test_agents_section_is_idempotent_and_preserves_human_text(tmp_path: Path):
    md = tmp_path / "AGENTS.md"
    md.write_text("# My repo\n\nHuman notes.\n")
    upsert_agents_md(tmp_path, PROGRAM)
    once = md.read_text()
    upsert_agents_md(tmp_path, PROGRAM)  # second run must not duplicate
    twice = md.read_text()
    assert once == twice
    assert "Human notes." in twice
    assert twice.count(AGENTS_SECTION_START) == 1
    assert AGENTS_SECTION_END in twice
    assert "AST" in twice and "events" in twice  # priority instructions present
```

- [ ] **Step 2: Run — expect failure**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_onboard.py -v`
Expected: FAIL (module missing).

- [ ] **Step 3: Implement**

```python
# src/active_harness/onboard.py
"""Program the harness: write harness.toml and the machine-managed AGENTS.md section."""

from pathlib import Path

from active_harness.config import HarnessProgram

AGENTS_SECTION_START = "<!-- ACTIVE-HARNESS:BEGIN (machine-managed) -->"
AGENTS_SECTION_END = "<!-- ACTIVE-HARNESS:END -->"


def write_program(root: Path, program: HarnessProgram) -> Path:
    """Serialize the harness program to ``root/harness.toml``."""
    lines = [f'language = "{program.language}"', ""]
    for c in program.checks:
        cmd = ", ".join(f'"{tok}"' for tok in c.command)
        lines += ["[[check]]", f'name = "{c.name}"', f"command = [{cmd}]", ""]
    path = root / "harness.toml"
    path.write_text("\n".join(lines))
    return path


def render_agents_section(program: HarnessProgram) -> str:
    """The instructions that make agents prioritize the harness."""
    return "\n".join(
        [
            AGENTS_SECTION_START,
            "## Active Harness — how to work here",
            "",
            f"Active language: **{program.language}**. Work through the harness, not the filesystem:",
            "",
            "- Navigate and edit code **only** via the AST tools (`ast_overview`, "
            "`ast_replace_node_body`, …). Never read or edit in-scope source as raw files.",
            "- **Do not call verification tools.** Checks run automatically after each edit; "
            "their results arrive as `events` on the next tool response — read those.",
            "- Respect the autonomy policy: gated (Tier-2) actions require explicit confirmation.",
            "- Drive process workflows via **superpowers** (brainstorming, writing-plans, TDD, "
            "systematic-debugging, subagent-driven-development).",
            AGENTS_SECTION_END,
        ]
    )


def upsert_agents_md(root: Path, program: HarnessProgram) -> Path:
    """Create or replace the marked section, preserving human-authored text."""
    path = root / "AGENTS.md"
    section = render_agents_section(program)
    existing = path.read_text() if path.exists() else ""
    if AGENTS_SECTION_START in existing and AGENTS_SECTION_END in existing:
        head, _, rest = existing.partition(AGENTS_SECTION_START)
        _, _, tail = rest.partition(AGENTS_SECTION_END)
        path.write_text(f"{head}{section}{tail}")
    else:
        sep = "" if existing.endswith("\n") or not existing else "\n\n"
        path.write_text(f"{existing}{sep}{section}\n")
    return path
```

- [ ] **Step 4: Green**

Run: `uv run --directory ~/dev/active-harness pytest tests/test_onboard.py -v`
Expected: PASS.

- [ ] **Step 5: Full suite + lint + typecheck + commit**

```bash
uv run --directory ~/dev/active-harness pytest -q && uv run --directory ~/dev/active-harness ruff check && uv run --directory ~/dev/active-harness ty check
git -C ~/dev/active-harness add src/active_harness/onboard.py tests/test_onboard.py
git -C ~/dev/active-harness commit -m "feat: onboard generator for harness.toml and AGENTS.md section"
```

---

## Self-Review

- **Spec coverage:** AST-as-interface → Tasks 3/6. Reactive engine → Tasks 4/5. Piggyback bus → Tasks 2/6. Onboard-programs-harness + AGENTS.md priority → Task 7. Deferred *by design* to later plans (noted, not gaps): strukturierter-Text-Modus, Tier-1 debounced local deploy + e2e selection, CI mirror, MCP Resources dual channel, absolute serena-gate on native reads, bundle plugin (claude-mem + superpowers). The skeleton proves the risky core chain only.
- **Placeholder scan:** none — every code step is complete.
- **Type consistency:** `Event`/`EventQueue`/`Check`/`HarnessProgram`/`react`/`envelope`/`Harness` names and signatures match across Tasks 2–7.

## Out of Scope for this plan (next plans, in order)

1. Absolute serena-gate: block native reads/edits of in-scope source; wire the two AST tools over real MCP stdio + an `/onboard` SKILL.md wrapper.
2. Tier-1 autonomy: debounced background execution, scoped-test selection with the "diminishing-returns + uniqueness" scorer, local deploy.
3. CI mirror resource; MCP Resources dual channel with `subscribe`.
4. Strukturierter-Text-Modus (configs/markdown/yaml).
5. Bundle plugin: core + claude-mem + superpowers.

## Verification (end-to-end, after Task 6)

The skeleton is proven when `tests/test_server.py::test_edit_reacts_and_event_arrives_on_next_read` is green: an AST edit introduces a lint error, no verification tool is called, and the error surfaces as a piggybacked event on the next read. `test_read_alone_runs_no_checks` guards the decoupling (reads never react).
