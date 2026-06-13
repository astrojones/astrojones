"""Tests for the harness MCP server's prompts SSOT.

The harness exposes per-repo workflow prompts (bugfix, feature, refactor, test,
implement, commit-semantic) and the /harness-init workflow body via @mcp.prompt
registration. These are the single source of truth that the Claude and opencode
plugin surfaces derive from.

`repo_prompt_get(name)` is the opencode-friendly wrapper around `prompts/get`
that returns the body as a JSON document (opencode's MCP integration surfaces
tools, not raw prompts, to the model).
"""

from __future__ import annotations

import asyncio
import json

from repo_agent_harness import server

# Names that must always be exposed. Adding a new prompt is a deliberate
# action: add it here too, or this test fails on review.
EXPECTED_PROMPTS: frozenset[str] = frozenset(
    {
        "bugfix",
        "feature",
        "refactor",
        "test",
        "implement",
        "commit-semantic",
        "harness-init",
    }
)


def test_expected_prompts_registered():
    """The full set of per-repo workflow prompts is registered on the server."""
    prompts = asyncio.run(server.mcp.list_prompts())
    names = {p.name for p in prompts}
    assert EXPECTED_PROMPTS <= names, (
        f"missing prompts: {sorted(EXPECTED_PROMPTS - names)}; "
        f"got: {sorted(names)}"
    )


def test_prompts_have_descriptions():
    """Every prompt advertises a non-empty description (for `prompts/list`)."""
    prompts = asyncio.run(server.mcp.list_prompts())
    for p in prompts:
        assert p.name in EXPECTED_PROMPTS, f"unexpected prompt: {p.name}"
        assert p.description, f"prompt {p.name!r} has empty description"


def test_repo_prompt_get_returns_body_and_metadata():
    """repo_prompt_get returns the body plus source-of-truth metadata."""
    result = server.repo_prompt_get("bugfix")
    assert result["ok"] is True
    assert result["name"] == "bugfix"
    assert isinstance(result["body"], str) and result["body"].strip()
    # Source-of-truth metadata: the relative path inside the package, so
    # drift-check tools can locate the on-disk file for comparison.
    assert result["source"] == "prompts/bugfix.md"
    assert "checksum" in result  # sha256 of the body, hex-encoded


def test_repo_prompt_get_unknown_returns_error():
    """Unknown prompt names return ok=False with a structured error, not an exception."""
    result = server.repo_prompt_get("does-not-exist")
    assert result["ok"] is False
    assert result["name"] == "does-not-exist"
    assert "available" in result  # the listing for the caller to suggest from


def test_repo_prompt_get_bodies_match_prompt_registry():
    """The body served by repo_prompt_get is the body the prompt registry serves.

    This is the SSOT contract: both call paths read the same on-disk file.
    """
    body = server.repo_prompt_get("feature")["body"]
    descriptor = asyncio.run(server.mcp.get_prompt("feature"))
    rendered = asyncio.run(descriptor.render())
    rendered_text = rendered.messages[0].content.text
    assert rendered_text == body


def test_prompt_bodies_have_no_assistant_specific_framing():
    """Prompt bodies are assistant-agnostic — no 'in Claude Code' / 'in opencode' language.

    The per-assistant framing lives in the plugin's `tools/` and the agent
    file, not in the prompt body. The prompt body is the workflow itself.
    """
    for name in EXPECTED_PROMPTS:
        body = server.repo_prompt_get(name)["body"]
        lower = body.lower()
        for forbidden in ("claude code", "opencode", "in this assistant"):
            assert forbidden not in lower, (
                f"prompt {name!r} contains assistant-specific framing ({forbidden!r}); "
                f"move it to the plugin surface, not the body"
            )


# ---------------------------------------------------------------------------
# CLI: `repo-agent-harness prompt get <name>` (used by the drift-check command
# and by humans reading a prompt without spinning up MCP)
# ---------------------------------------------------------------------------


def test_cli_prompt_get_subcommand(capsys, repo, monkeypatch):
    """`prompt get <name>` prints the body to stdout as JSON."""
    from repo_agent_harness import cli

    monkeypatch.chdir(repo)
    code = cli.main(["prompt", "get", "bugfix"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["ok"]
    assert out["name"] == "bugfix"
    assert out["body"].strip()


def test_cli_prompt_list_subcommand(capsys, repo, monkeypatch):
    """`prompt list` prints the registered prompt names as JSON."""
    from repo_agent_harness import cli

    monkeypatch.chdir(repo)
    code = cli.main(["prompt", "list"])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert set(out["prompts"]) >= EXPECTED_PROMPTS
