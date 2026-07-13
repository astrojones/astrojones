"""Static symbol index: extraction, lazy mtime freshness, persistence, scoping, caps."""

import os
import subprocess

from repo_agent_harness import symbols
from repo_agent_harness.symbols import SymbolsOverviewIn


def _commit_file(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    subprocess.run(["git", "add", rel], cwd=repo, check=True, capture_output=True)


def test_overview_maps_python_symbols_with_nesting(repo, monkeypatch):
    out = symbols.overview(str(repo), SymbolsOverviewIn())
    assert out.indexed_files >= 3  # payment.py, util.py, tests/test_payment.py
    payment = {s.name: s for s in out.symbols["src/payment.py"]}
    assert payment["charge"].kind == "function"
    assert payment["charge"].start_line == 1
    assert payment["charge"].parent is None


def test_overview_extracts_typescript_and_shell(repo):
    _commit_file(
        repo,
        "app/main.ts",
        "export class Api {\n  handle(): void {}\n}\ninterface Cfg { url: string }\nfunction boot() {}\n",
    )
    _commit_file(repo, "tools/run.sh", "#!/bin/bash\ndeploy() {\n  echo hi\n}\n")
    out = symbols.overview(str(repo), SymbolsOverviewIn())
    ts = {(s.name, s.kind, s.parent) for s in out.symbols["app/main.ts"]}
    assert ("Api", "class", None) in ts
    assert ("handle", "method", "Api") in ts
    assert ("Cfg", "interface", None) in ts
    assert ("boot", "function", None) in ts
    sh = {(s.name, s.kind) for s in out.symbols["tools/run.sh"]}
    assert ("deploy", "function") in sh


def test_lazy_refresh_reparses_only_changed_files(repo):
    first = symbols.overview(str(repo), SymbolsOverviewIn())
    assert first.reparsed_files == first.indexed_files  # full build on first use
    second = symbols.overview(str(repo), SymbolsOverviewIn())
    assert second.reparsed_files == 0  # nothing changed -> pure cache read
    target = repo / "src" / "payment.py"
    target.write_text("def charge():\n    return 1\n\n\ndef refund():\n    return 2\n")
    os.utime(target, (0, 0))  # force an mtime that differs from the stored one
    third = symbols.overview(str(repo), SymbolsOverviewIn())
    assert third.reparsed_files == 1  # only the edited file
    names = {s.name for s in third.symbols["src/payment.py"]}
    assert "refund" in names


def test_index_persists_across_instances(repo):
    symbols.overview(str(repo), SymbolsOverviewIn())
    assert symbols._index_file(str(repo)).is_file()
    # A "fresh process" (new load) reuses the stored parse results.
    out = symbols.overview(str(repo), SymbolsOverviewIn())
    assert out.reparsed_files == 0


def test_path_scoping_and_limit(repo):
    out = symbols.overview(str(repo), SymbolsOverviewIn(path="src"))
    assert set(out.symbols) == {"src/payment.py", "src/util.py"}
    capped = symbols.overview(str(repo), SymbolsOverviewIn(limit=1))
    assert capped.truncated is True
    assert sum(len(v) for v in capped.symbols.values()) == 1


def test_untracked_and_foreign_files_are_ignored(repo):
    (repo / "scratch.py").write_text("def hidden(): ...\n")  # untracked -> not indexed
    _commit_file(repo, "README.md", "# readme\n")  # tracked, non-indexed language
    out = symbols.overview(str(repo), SymbolsOverviewIn())
    assert "scratch.py" not in out.symbols
    assert "README.md" not in out.symbols


def test_oversized_file_is_skipped(repo, monkeypatch):
    monkeypatch.setattr(symbols, "_PARSE_MAX_BYTES", 10)
    assert symbols.parse_file(str(repo), "src/payment.py") == []


def test_deleted_file_drops_from_index(repo):
    symbols.overview(str(repo), SymbolsOverviewIn())
    (repo / "src" / "util.py").unlink()
    subprocess.run(["git", "rm", "-q", "src/util.py"], cwd=repo, check=True, capture_output=True)
    out = symbols.overview(str(repo), SymbolsOverviewIn())
    assert "src/util.py" not in out.symbols


def test_python_docstring_first_line_is_indexed(repo):
    _commit_file(
        repo,
        "src/documented.py",
        '''def greet():
    """Say hello to the world.

    Longer explanation that must not be captured.
    """
    return "hi"


class Widget:
    """A small UI widget."""

    def render(self):
        return None
''',
    )
    out = symbols.overview(str(repo), SymbolsOverviewIn())
    docs = {(s.name, s.parent): s.doc for s in out.symbols["src/documented.py"]}
    assert docs["greet", None] == "Say hello to the world."
    assert docs["Widget", None] == "A small UI widget."
    # A method with no docstring yields None.
    assert docs["render", "Widget"] is None


def test_symbol_without_docstring_has_none_doc(repo):
    # The fixture's charge() has no docstring.
    records = symbols.parse_file(str(repo), "src/payment.py")
    assert {r.name: r.doc for r in records} == {"charge": None}
