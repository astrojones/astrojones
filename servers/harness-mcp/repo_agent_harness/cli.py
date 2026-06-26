"""Umbrella CLI exposing the harness core for terminals, CI, and agent/tools shims.

Every subcommand emits JSON and exits non-zero when a check reports ``ok: false``.
``--json`` is accepted everywhere (output is always JSON) for shim compatibility.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repo_agent_harness import (
    agent_hooks,
    context,
    deploy,
    drift,
    gateway,
    git,
    health,
    impact,
    policies,
    prompts_registry,
    scaffold,
    verify,
)


def _root() -> str:
    root = git.repo_root()
    if not root:
        print(json.dumps({"error": "not inside a git repository"}))
        raise SystemExit(2)
    return root


def _hook(event: str) -> int:
    """Run a Claude Code hook handler: event JSON on stdin, decision JSON on stdout.

    Fail-open by contract: any error (bad JSON, missing repo, policy bug) prints an
    empty response and exits 0, so a hook problem never blocks legitimate work.
    """
    handlers = {
        "pre-tool-use": agent_hooks.pre_tool_use,
        "post-tool-use": agent_hooks.post_tool_use,
        "user-prompt-submit": agent_hooks.user_prompt_submit,
    }
    try:
        data = json.load(sys.stdin)
        out = handlers.get(event, agent_hooks.pre_tool_use)(data)
    except Exception:  # noqa: BLE001 — fail-open contract: any hook error must yield an empty allow
        out = {}
    print(json.dumps(out))
    return 0


def _deploy_status(limit: int, root: str) -> dict:
    """CLI wrapper for `repo_deploy_status` — list recent deploy runs."""
    name = deploy.repo_name(Path(root), None)
    return deploy.status(name, limit)


def _deploy_logs(run_id: str, tail: int, root: str) -> dict:
    """CLI wrapper for `repo_deploy_logs` — fetch failed-step logs of a run."""
    name = deploy.repo_name(Path(root), None)
    return deploy.logs(name, run_id, tail)


def main(argv: list[str] | None = None) -> int:
    """CLI entry point: dispatch subcommand and print JSON.

    Args:
        argv: Argument list; defaults to sys.argv when None.

    Returns:
        0 on success, 1 when a check reports ok=False, 2 when not in a repo.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--json",
        action="store_true",
        help="emit JSON (default; accepted for compatibility)",
    )

    p = argparse.ArgumentParser(
        prog="repo-agent-harness",
        parents=[common],
        description="repo-agent-harness CLI",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("overview", parents=[common])
    sub.add_parser("status", parents=[common])
    sp = sub.add_parser("relevant-files", parents=[common])
    sp.add_argument("task")
    sp.add_argument("--max-files", type=int, default=8)
    sp = sub.add_parser("search-text", parents=[common])
    sp.add_argument("pattern")
    sp.add_argument("paths", nargs="*")
    sp.add_argument("--limit", type=int, default=20)
    sp = sub.add_parser("search-files", parents=[common])
    sp.add_argument("pattern")
    sp.add_argument("--limit", type=int, default=20)
    sp = sub.add_parser("read-range", parents=[common])
    sp.add_argument("path")
    sp.add_argument("--start", type=int, default=1)
    sp.add_argument("--end", type=int, default=200)
    sp = sub.add_parser("impact", parents=[common])
    sp.add_argument("path")
    sub.add_parser("verify-changed", parents=[common])
    sub.add_parser("lint-changed", parents=[common])
    sub.add_parser("typecheck-changed", parents=[common])
    sub.add_parser("test-changed", parents=[common])
    sp = sub.add_parser("diff", parents=[common])
    sp.add_argument("--context-lines", type=int, default=3)
    sp = sub.add_parser("check-command", parents=[common])
    sp.add_argument("command")
    sp = sub.add_parser("deploy-validate", parents=[common])
    sp.add_argument("--root", default=None)
    sp.add_argument("--repo", default=None)
    sp = sub.add_parser("deploy-status", parents=[common])
    sp.add_argument("--limit", type=int, default=5)
    sp = sub.add_parser("deploy-logs", parents=[common])
    sp.add_argument("run_id")
    sp.add_argument("--tail", type=int, default=200)
    sp = sub.add_parser(
        "drift-check",
        parents=[common],
        help="compare harness prompt bodies to on-disk SKILL.md copies (warning only, never errors)",
    )
    sp = sub.add_parser(
        "sync-prompts",
        parents=[common],
        help="refresh SKILL.md copies to match the harness (idempotent; --force overwrites in-sync files)",
    )
    sp.add_argument("--force", action="store_true", help="overwrite even in-sync files")
    sub.add_parser(
        "gateway-snapshot",
        parents=[common],
        help="(dev) launch the pinned Serena once and regenerate the packaged tool snapshot",
    )
    sp = sub.add_parser("health", parents=[common])
    sp.add_argument("--check", default=None, help="run only this check id from agent/health.yml")
    sp.add_argument("--refresh", action="store_true", help="bypass the cache and re-run all checks")
    sp = sub.add_parser(
        "init",
        parents=[common],
        help="install the per-repo harness template (agent/, AGENTS.md, .mcp.json)",
    )
    sp.add_argument("--agents-md", choices=["auto", "skip", "overwrite"], default="auto")
    sp.add_argument("--force", action="store_true", help="overwrite existing template files")
    sp.add_argument("--pin", default=None, help="commit sha to pin the harness spec in .mcp.json")
    sp.add_argument("--spec", default=None, help="override the full harness requirement spec")
    sp = sub.add_parser(
        "bootstrap",
        parents=[common],
        help=(
            "first-touch materialization of the per-repo harness "
            "(claude | opencode | both); plugin's load-time hook calls this"
        ),
    )
    sp.add_argument(
        "--target",
        choices=["claude", "opencode", "both"],
        default="claude",
        help="which per-assistant surface to materialize (default: claude)",
    )
    sp.add_argument(
        "--agents-md",
        choices=["auto", "skip", "overwrite"],
        default="skip",
        help="AGENTS.md handling: auto (append section), overwrite (replace), skip (default)",
    )
    sp.add_argument("--force", action="store_true", help="overwrite existing harness-managed files")
    sp.add_argument("--pin", default=None, help="commit sha to pin the harness spec in .mcp.json")
    sp.add_argument("--spec", default=None, help="override the full harness requirement spec")
    sp = sub.add_parser(
        "hook",
        parents=[common],
        help="Claude Code hook handler: read the event JSON on stdin, print the decision (always exits 0)",
    )
    sp.add_argument("event", choices=["pre-tool-use", "post-tool-use", "user-prompt-submit"])
    sp = sub.add_parser(
        "prompt",
        parents=[common],
        help="Inspect the per-repo workflow prompts SSOT (works without a git repo)",
    )
    prompt_sub = sp.add_subparsers(dest="prompt_cmd", required=True)
    prompt_sub.add_parser("list", parents=[common], help="List registered prompt names")
    sp_get = prompt_sub.add_parser("get", parents=[common], help="Print a single prompt body as JSON")
    sp_get.add_argument("name", help="Prompt identifier (e.g. 'bugfix', 'harness-init')")

    args = p.parse_args(argv)

    if args.cmd == "hook":  # before _root(): the hook must fail open outside a repo too
        return _hook(args.event)

    if args.cmd == "prompt":  # before _root(): prompts are package-local, no repo needed
        if args.prompt_cmd == "list":
            data = {"ok": True, "prompts": prompts_registry.list_names()}
        else:  # get
            entry = prompts_registry.get(args.name)
            if entry is None:
                data = {
                    "ok": False,
                    "name": args.name,
                    "error": f"unknown prompt: {args.name!r}",
                    "available": prompts_registry.list_names(),
                }
            else:
                data = {
                    "ok": True,
                    "name": entry.name,
                    "title": entry.title,
                    "description": entry.description,
                    "body": entry.body,
                    "source": entry.source,
                    "checksum": entry.checksum,
                }
        print(json.dumps(data, indent=2))
        return 1 if isinstance(data, dict) and data.get("ok") is False else 0

    root = _root()

    dispatch = {
        "overview": lambda: context.overview(root),
        "status": lambda: git.status(root),
        "relevant-files": lambda: context.relevant_files(root, args.task, args.max_files),
        "search-text": lambda: context.search_text(root, args.pattern, args.paths or None, args.limit),
        "search-files": lambda: context.search_files(root, args.pattern, args.limit),
        "read-range": lambda: context.read_range(root, args.path, args.start, args.end),
        "impact": lambda: impact.file_impact(root, args.path),
        "verify-changed": lambda: verify.verify_changed(root),
        "lint-changed": lambda: verify.lint_changed(root),
        "typecheck-changed": lambda: verify.typecheck_changed(root),
        "test-changed": lambda: verify.test_changed(root),
        "diff": lambda: git.diff_current(root, args.context_lines),
        "check-command": lambda: policies.check_command(args.command, root).to_dict(),
        "deploy-validate": lambda: deploy.validate(
            Path(args.root) if args.root else Path(root),
            args.repo or deploy.repo_name(Path(args.root) if args.root else Path(root), None),
        ),
        "deploy-status": lambda: _deploy_status(args.limit, root),
        "deploy-logs": lambda: _deploy_logs(args.run_id, args.tail, root),
        "drift-check": lambda: drift.check_repo_drift(root),
        "sync-prompts": lambda: drift.sync_prompts(root, force=args.force),
        "health": lambda: health.run(root, only=args.check, refresh=args.refresh).model_dump(),
        "gateway-snapshot": lambda: gateway.generate_snapshot(root),
        "init": lambda: scaffold.init_repo(
            root,
            agents_md=args.agents_md,
            force=args.force,
            pin=args.pin,
            spec=args.spec,
        ),
        "bootstrap": lambda: scaffold.bootstrap_repo(
            root,
            target=args.target,
            agents_md=args.agents_md,
            force=args.force,
            pin=args.pin,
            spec=args.spec,
        ),
    }
    data = dispatch[args.cmd]()
    print(json.dumps(data, indent=2))
    return 1 if isinstance(data, dict) and data.get("ok") is False else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
