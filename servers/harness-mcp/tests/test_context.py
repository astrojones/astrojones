import pytest
from repo_agent_harness import context


def test_overview(repo):
    ov = context.overview(str(repo))
    assert ov["root"] == str(repo)
    assert "Python" in ov["languages"]
    assert any("pyproject" in p for p in ov["package_managers"])


def test_detect_languages_orders_by_count(repo):
    """Languages are reported most-prevalent first; a lone secondary language still appears."""
    (repo / "web").mkdir()
    (repo / "web" / "a.ts").write_text("export const a = 1\n")
    (repo / "web" / "b.tsx").write_text("export const b = 2\n")
    langs = context.detect_languages(str(repo))
    assert langs[0] == "Python"  # 3 .py files dominate the 2 .ts/.tsx files
    assert "TypeScript" in langs


def test_serena_languages_maps_and_dedupes(repo):
    """.ts/.tsx and .js all map to the typescript server (de-duplicated); .c maps to cpp."""
    (repo / "a.ts").write_text("export const a = 1\n")
    (repo / "b.js").write_text("const b = 2\n")
    (repo / "c.c").write_text("int main(void){return 0;}\n")
    keys = context.serena_languages(str(repo))
    assert keys.count("typescript") == 1
    assert "python" in keys
    assert "cpp" in keys


def test_overview_configured_tools_present(repo):
    ov = context.overview(str(repo))
    assert "configured_tools" in ov
    assert isinstance(ov["configured_tools"], dict)


def test_overview_configured_tools_reads_pyproject(repo):
    (repo / "pyproject.toml").write_text(
        "[project]\nname='x'\n[tool.ty.rules]\n[tool.ruff]\n[tool.pytest.ini_options]\n"
    )
    ov = context.overview(str(repo))
    assert ov["configured_tools"] == {"typecheck": "ty", "lint": "ruff", "test": "pytest"}


def test_overview_harness_absent(repo):
    """A bare repo is not harnessed and surfaces no harness inventory."""
    h = context.overview(str(repo))["harness"]
    assert h["harnessed"] is False
    assert h["guide"] is None
    assert h["policies"] == []
    assert h["tools"] == []
    assert h["agents"] == []
    assert h["skills"] == []


def test_overview_harness_present(repo):
    """When agent/ + the AGENTS.md workflow section + plugin dirs exist, surface them."""
    from repo_agent_harness import scaffold

    (repo / "AGENTS.md").write_text(f"# guide\n{scaffold.SECTION_BEGIN}\nworkflow\n{scaffold.SECTION_END}\n")
    (repo / "agent" / "policies").mkdir(parents=True)
    (repo / "agent" / "policies" / "shell.yml").write_text("rules: []\n")
    (repo / "agent" / "tools").mkdir()
    (repo / "agent" / "tools" / "safe-diff").write_text("#!/bin/sh\n")
    (repo / "agents").mkdir()
    (repo / "agents" / "fullstack-architect.md").write_text("---\nname: x\n---\n")
    (repo / "agents" / "_base.md").write_text("ignored: leading underscore\n")
    (repo / "skills").mkdir()
    (repo / "skills" / "nuklaut-deploy").mkdir()

    h = context.overview(str(repo))["harness"]
    assert h["harnessed"] is True
    assert h["guide"] == "AGENTS.md"
    assert h["policies"] == ["shell"]
    assert h["tools"] == ["safe-diff"]
    assert h["agents"] == ["fullstack-architect"]
    assert h["skills"] == ["nuklaut-deploy"]


def test_read_range_ok(repo):
    out = context.read_range(str(repo), "src/payment.py", 1, 2)
    assert "def charge" in out["content"]
    assert out["start_line"] == 1


def test_read_range_refuses_secret(repo):
    out = context.read_range(str(repo), ".env", 1, 5)
    assert "error" in out


def test_read_range_blocks_traversal(repo):
    with pytest.raises(ValueError):
        context.resolve_within_repo(str(repo), "../../etc/passwd")


def test_read_range_caps_lines(repo):
    big = repo / "big.py"
    big.write_text("\n".join(f"x{i} = {i}" for i in range(1000)) + "\n")
    out = context.read_range(str(repo), "big.py", 1, 999)
    assert out["truncated"] is True


def test_search_files(repo):
    out = context.search_files(str(repo), "*.py")
    assert "src/payment.py" in out["files"]


def test_search_text_finds_and_redacts(repo):
    out = context.search_text(str(repo), "charge")
    assert any(m["path"] == "src/payment.py" for m in out["matches"])


def test_relevant_files(repo):
    out = context.relevant_files(str(repo), "fix payment charge bug")
    paths = [f["path"] for f in out["files"]]
    assert any("payment" in p for p in paths)
