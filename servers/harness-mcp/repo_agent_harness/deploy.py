"""Validate this repo's nuklaut deploy files against the org's hard rules.

Lifted from the plugin's ``template/_shared/agent/tools/deploy-validate``
script and adapted for direct import (no longer needs to be ``python3 script.py``).
The rules match the ``nuklaut-deploy`` skill exactly; this is the programmatic
form the same checks take when invoked via ``repo_deploy_validate`` MCP tool
or ``repo-agent-harness deploy-validate`` CLI subcommand.

Self-contained (stdlib only) so it works in any scaffolded app — Python or
Node — with no project env. Line-based parsing targets the org template
shape; full-line comments are ignored.

Exit 0 = deployable (warnings allowed), 1 = rule violations, 2 = cannot run.
"""

from __future__ import annotations

import json
import re
import subprocess
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

# Built by concatenation so this file never contains the literal tokens it hunts.
PLACEHOLDER_RE = re.compile("__" + "REPO_" + "(NAME|PKG)" + "__")
# Org is derived from the repo's git origin owner (or an explicit override); the reusable
# workflow lives at <owner>/.github/.github/workflows/nuk-deploy.yml. No org is hardcoded.
WORKFLOW_REF_SUFFIX = "/.github/.github/workflows/nuk-deploy.yml"
SKIP_DIRS = {".git", ".venv", "node_modules", "__pycache__", ".serena", ".pytest_cache"}


def _lines(text: str) -> list[str]:
    """Lines with full-line comments dropped (template files comment heavily)."""
    return [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]


def _strip_inline_comment(line: str) -> str:
    return line.split("#", 1)[0]


def _origin_parts(root: Path) -> tuple[str | None, str | None]:
    """(owner, repo) parsed from `git -C root remote get-url origin`, or (None, None).

    Handles both ssh (``git@github.com:owner/repo.git``) and https forms.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        url = proc.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if proc.returncode != 0 or not url:
        return None, None
    slug = url.rstrip("/").removesuffix(".git").replace(":", "/")  # git@github.com:o/r -> .../o/r
    parts = [p for p in slug.split("/") if p]
    match parts:
        case [*_, owner, name]:
            return owner, name
        case _:
            return None, None


def repo_name(root: Path, override: str | None) -> str:
    """The repo name, in priority order: override > origin URL > dir name."""
    if override:
        return override
    _, name = _origin_parts(root)
    return name or root.resolve().name


def origin_owner(root: Path, override: str | None) -> str | None:
    """The GitHub org/owner: override > origin URL owner > None (org unverifiable)."""
    if override:
        return override
    owner, _ = _origin_parts(root)
    return owner


def _find_manifest(root: Path) -> tuple[Path | None, bool]:
    """Locate the nuk manifest: (path, is_legacy).

    Prefers the repo-root ``deployment.yml``; falls back to the deprecated
    ``.nuklaut/deployment.yml`` (``is_legacy=True``). Returns ``(None, False)``
    when neither exists.
    """
    root_manifest = root / "deployment.yml"
    if root_manifest.is_file():
        return root_manifest, False
    legacy = root / ".nuklaut" / "deployment.yml"
    if legacy.is_file():
        return legacy, True
    return None, False


def _app_host(root: Path) -> str | None:
    """The app's ingress host (e.g. ``<repo>.<domain>``) from its own manifest.

    Derived from the deployed manifest so the URL stays self-consistent with the
    real host — no domain is hardcoded. Returns None when unavailable.
    """
    path, _ = _find_manifest(root)
    if path is None:
        return None
    for entry in parse_manifest(path.read_text()).get("ingress", []):
        if entry.get("host"):
            return entry["host"]
    return None


def status(root: Path, name: str, limit: int, owner: str | None) -> dict:
    """Recent deploy runs + the app's published URL for repo ``name``.

    Shared by ``repo_deploy_status`` (MCP) and the ``deploy-status`` CLI so the
    error shapes never drift. The app URL is derived from the manifest's ingress
    host and the image from the git origin owner — nothing is hardcoded. Thin
    wrapper over ``gh run list`` — never raises: returns a structured
    ``{error, hint}`` on missing/unauthenticated ``gh``.
    """
    host = _app_host(root)
    base = {
        "repo": name,
        "app_url": f"https://{host}" if host else None,
        "image": f"ghcr.io/{owner}/{name}:latest" if owner else None,
    }
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            [
                "gh",
                "run",
                "list",
                "--workflow",
                "deploy.yml",
                "--limit",
                str(limit),
                "--json",
                "databaseId,status,conclusion,displayTitle,headSha,updatedAt,url",
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except FileNotFoundError:
        return {
            **base,
            "error": "gh CLI not found in PATH",
            "hint": "Install gh: https://cli.github.com/, then `gh auth login`.",
            "runs": [],
        }
    if proc.returncode != 0:
        return {
            **base,
            "error": "gh run list failed",
            "gh_stderr": proc.stderr.strip(),
            "hint": "Run `gh auth status` to check authentication.",
            "runs": [],
        }
    return {**base, "runs": json.loads(proc.stdout or "[]")}


def logs(name: str, run_id: str, tail: int, owner: str | None) -> dict:
    """Failed-step logs of a deploy run for repo ``name``.

    Shared by ``repo_deploy_logs`` (MCP) and the ``deploy-logs`` CLI. The repo
    slug is built from the git origin owner (``<owner>/<name>``); when the owner
    is unknown, ``--repo`` is omitted so ``gh`` infers it from the cwd. Thin
    wrapper over ``gh run view --log-failed`` — never raises; returns a
    structured error on missing gh / unauthenticated / run-not-found.
    """
    argv = ["gh", "run", "view", run_id]
    if owner:
        argv += ["--repo", f"{owner}/{name}"]
    argv.append("--log-failed")
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell
            argv,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except FileNotFoundError:
        return {
            "repo": name,
            "run_id": run_id,
            "error": "gh CLI not found in PATH",
            "hint": "Install gh: https://cli.github.com/, then `gh auth login`.",
        }
    if proc.returncode != 0:
        return {
            "repo": name,
            "run_id": run_id,
            "error": "gh run view failed",
            "gh_stderr": proc.stderr.strip(),
            "logs": proc.stdout[-8000:] if proc.stdout else "",  # last chunk on partial success
        }
    return {
        "repo": name,
        "run_id": run_id,
        "logs": "\n".join(proc.stdout.splitlines()[-tail:]),
    }


def iter_text_files(root: Path):
    """Yield (path, text) for every readable text file under root."""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            raw = path.read_bytes()[: 1 << 20]
        except OSError:
            continue
        if b"\0" in raw[:1024]:
            continue
        yield path, raw.decode("utf-8", errors="replace")


def parse_compose(text: str) -> dict[str, dict]:
    """Map of service name -> {image, expose} from a template-shaped compose file."""
    services: dict[str, dict] = {}
    current = None
    in_services = in_expose = False
    for line in _lines(text):
        if re.match(r"^services:\s*$", _strip_inline_comment(line).rstrip()):
            in_services = True
            continue
        if in_services and line and not line[0].isspace():
            in_services = False
        if not in_services:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", _strip_inline_comment(line).rstrip())
        if m:
            current = m.group(1)
            services[current] = {"image": None, "expose": []}
            in_expose = False
            continue
        if current is None:
            continue
        bare = _strip_inline_comment(line)
        mi = re.match(r"^\s+image:\s*(\S+)", bare)
        if mi:
            services[current]["image"] = mi.group(1)
        if re.match(r"^\s+expose:", bare):
            in_expose = True
            continue
        if in_expose:
            me = re.match(r"^\s+-\s*[\"']?(\d+)", bare)
            if me:
                services[current]["expose"].append(me.group(1))
                continue
            if bare.strip():
                in_expose = False
    return services


def parse_manifest(text: str) -> dict:
    """apiVersion, metadata.name, and ingress (service, port) pairs; flow or block style."""
    out: dict = {"api_version": None, "name": None, "ingress": []}
    lines = _lines(text)
    m = re.search(r"^apiVersion:\s*(\S+)", "\n".join(lines), re.M)
    out["api_version"] = m.group(1) if m else None
    in_meta = False
    for line in lines:
        if re.match(r"^metadata:\s*$", line.rstrip()):
            in_meta = True
            continue
        if in_meta:
            mn = re.match(r"^\s+name:\s*(\S+)", _strip_inline_comment(line))
            if mn:
                out["name"] = mn.group(1)
                break
            if line and not line[0].isspace():
                break
    ingress_indent = None
    items: list[str] = []
    for line in lines:
        bare = _strip_inline_comment(line).rstrip()
        if re.match(r"^\s*ingress:\s*$", bare):
            ingress_indent = len(line) - len(line.lstrip())
            continue
        if ingress_indent is None:
            continue
        indent = len(line) - len(line.lstrip())
        if bare.strip() and indent <= ingress_indent:
            ingress_indent = None
            continue
        if re.match(r"^\s*-", bare):
            items.append(bare)
        elif items and bare.strip():
            items[-1] += " " + bare.strip()
    for item in items:
        ms = re.search(r"service:\s*([A-Za-z0-9_-]+)", item)
        mp = re.search(r"port:\s*[\"']?(\d+)", item)
        mh = re.search(r"host:\s*(\S+)", item)
        if ms or mp or mh:
            out["ingress"].append({
                "service": ms.group(1) if ms else None,
                "port": mp.group(1) if mp else None,
                "host": mh.group(1) if mh else None,
            })
    return out


def validate(root: Path, repo: str, owner: str | None) -> dict:
    """Run all hard rules against ``root``. Returns the standard result dict.

    ``owner`` is the GitHub org (from the git origin, or an override). When it is
    None the org cannot be verified, so the image-owner and workflow-org checks
    degrade to shape-only warnings ("org unverified") instead of hard failures.
    """
    findings: list[dict] = []

    def err(code: str, message: str) -> None:
        findings.append({"level": "error", "code": code, "message": message})

    def warn(code: str, message: str) -> None:
        findings.append({"level": "warning", "code": code, "message": message})

    def ok(code: str, message: str) -> None:
        findings.append({"level": "ok", "code": code, "message": message})

    if not root.is_dir():
        err("root", f"{root} is not a directory")
        return {"ok": False, "repo": repo, "root": str(root), "findings": findings}

    hits = [f"{path.relative_to(root)}" for path, text in iter_text_files(root) if PLACEHOLDER_RE.search(text)]
    if hits:
        err("placeholder", f"unreplaced template placeholder in: {', '.join(hits[:10])}")
    else:
        ok("placeholder", "no template placeholders left")

    compose_path = root / "docker-compose.yml"
    services: dict[str, dict] = {}
    if not compose_path.is_file():
        err("compose", "docker-compose.yml not found")
    else:
        text = compose_path.read_text()
        for line in _lines(text):
            bare = _strip_inline_comment(line)
            if re.search(r"(^|\s)ports:", bare):
                err("compose-forbidden", "compose uses `ports:` — use `expose:` (Traefik routes internally)")
            if re.search(r"(^|\s)container_name:", bare):
                err(
                    "compose-forbidden",
                    "compose sets `container_name:` — remove it (nuk names containers)",
                )
            if "traefik." in bare:
                err(
                    "compose-forbidden",
                    "compose has `traefik.*` labels — remove them (nuk generates routing)",
                )
        services = parse_compose(text)
        images = {name: svc["image"] for name, svc in services.items() if svc["image"]}
        if not images:
            err("image", "no `image:` found under services")
        elif owner:
            expected = f"ghcr.io/{owner}/{repo}:latest"
            for name, image in images.items():
                if image != expected:
                    err(
                        "image",
                        f"service `{name}` pulls `{image}` — CI pushes `{expected}` (two segments, repo name)",
                    )
            if all(img == expected for img in images.values()):
                ok("image", f"image is {expected}")
        else:
            # No git origin / override: verify the two-segment shape but not the org.
            shape = re.compile(rf"^ghcr\.io/[^/]+/{re.escape(repo)}:latest$")
            for name, image in images.items():
                if not shape.match(image):
                    err("image", f"service `{name}` pulls `{image}` — expected ghcr.io/<owner>/{repo}:latest")
            if all(shape.match(img) for img in images.values()):
                warn("image", f"image shape ok (ghcr.io/<owner>/{repo}:latest); org unverified — pass an owner")

    manifest_path, manifest_legacy = _find_manifest(root)
    manifest: dict = {"ingress": []}
    if manifest_path is None:
        err("manifest", "deployment.yml not found at repo root (legacy .nuklaut/deployment.yml also absent)")
    else:
        if manifest_legacy:
            warn(
                "manifest-legacy",
                "manifest read from deprecated .nuklaut/deployment.yml — move it to the repo-root deployment.yml",
            )
        manifest = parse_manifest(manifest_path.read_text())
        if manifest["api_version"] != "nuk/v1":
            err("manifest", f"apiVersion is {manifest['api_version']!r}; must be nuk/v1")
        if manifest["name"] != repo:
            err(
                "manifest-name",
                f"metadata.name is {manifest['name']!r}; must equal the repo name {repo!r}",
            )
        else:
            ok("manifest-name", f"metadata.name == {repo}")
        if not manifest["ingress"]:
            warn("ingress", "no ingress entries found — app will not be routable")
        for entry in manifest["ingress"]:
            svc, port = entry["service"], entry["port"]
            if services and svc and svc not in services:
                err(
                    "ingress-service",
                    f"ingress references service `{svc}` not defined in docker-compose.yml",
                )
            elif svc in services and port and port not in services[svc]["expose"]:
                warn(
                    "expose-port",
                    f"ingress port {port} is not in service `{svc}` `expose:` list",
                )

    workflow_path = root / ".github" / "workflows" / "deploy.yml"
    if not workflow_path.is_file():
        err("workflow", ".github/workflows/deploy.yml not found")
    else:
        uses = [ln for ln in _lines(workflow_path.read_text()) if "uses:" in ln and WORKFLOW_REF_SUFFIX in ln]
        if owner:
            workflow_ref = f"{owner}{WORKFLOW_REF_SUFFIX}"
            if any(workflow_ref in ln for ln in uses):
                ok("workflow", "deploy.yml calls the org reusable workflow")
            else:
                err("workflow", f"deploy.yml does not call the reusable workflow {workflow_ref}")
        elif uses:
            warn("workflow", f"deploy.yml calls a <owner>{WORKFLOW_REF_SUFFIX}; org unverified — pass an owner")
        else:
            err("workflow", f"deploy.yml does not call a reusable workflow at <owner>{WORKFLOW_REF_SUFFIX}")

    dockerfile = root / "Dockerfile"
    if not dockerfile.is_file():
        err("dockerfile", "Dockerfile not found")
    else:
        exposed = re.findall(r"^EXPOSE\s+(\d+)", dockerfile.read_text(), re.M)
        for entry in manifest["ingress"]:
            if entry["port"] and exposed and entry["port"] not in exposed:
                warn(
                    "dockerfile-expose",
                    f"Dockerfile EXPOSE {exposed} does not include ingress port {entry['port']}",
                )

    is_ok = not any(f["level"] == "error" for f in findings)
    return {"ok": is_ok, "repo": repo, "root": str(root), "findings": findings}
