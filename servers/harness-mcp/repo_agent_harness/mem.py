"""Business logic for the ``mem_*`` durable-memory tools (remote cognee).

Public signatures speak pydantic: every function takes its ``Mem*In`` input model and
returns a ``Mem*Result | MemError`` — client failures are mapped to :class:`MemError`
instead of raising, so the MCP bindings in server.py stay one-liners (``model_dump`` at
the protocol boundary) and the agent always gets an actionable message.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from http import HTTPStatus
from pathlib import Path
from xml.sax.saxutils import escape, quoteattr

from repo_agent_harness import cognee_client
from repo_agent_harness.cognee_client import (
    NOT_CONFIGURED_HINT,
    CogneeClient,
    CogneeError,
    CogneeNotConfiguredError,
    CogneeUnavailableError,
)
from repo_agent_harness.models import (
    MemDoctorResult,
    MemError,
    MemIngestEstimate,
    MemIngestIn,
    MemIngestResult,
    MemMigrateResult,
    MemOntologyIn,
    MemOntologyResult,
    MemRememberIn,
    MemRememberResult,
    MemSearchIn,
    MemSearchResult,
    MemStatsIn,
    MemStatsResult,
)

DEFAULT_DATASET = "agent_sessions"

# Cost pre-flight for bulk ingest: chars/4 ~ tokens; each chunk re-passes through the
# extraction LLM, so price ~ tokens * usd-per-Mtok. Both knobs are env-tunable; the limit
# is deliberately conservative — an over-limit run needs an explicit confirm=True.
_CHARS_PER_TOKEN = 4
_CHUNK_TOKENS = 1024  # cognee's default chunk_size order of magnitude
_USD_PER_MTOK_ENV = "COGNEE_INGEST_USD_PER_MTOK"
_COST_LIMIT_ENV = "COGNEE_INGEST_COST_LIMIT_USD"
_DEFAULT_USD_PER_MTOK = 5.0
_DEFAULT_COST_LIMIT_USD = 1.0

# Sentinels for competing capture pipelines (mem_doctor): if these are live, memory is
# being double-written by a plugin that should have been disabled in the cutover.
_CLAUDE_MEM_DB = Path("~/.claude-mem/claude-mem.db")
_COGNEE_PLUGIN_DIR = Path("~/.cognee-plugin")
_SENTINEL_RECENT_S = 600


def _client(client: CogneeClient | None) -> CogneeClient:
    return client if client is not None else cognee_client.get_client()


def _error(exc: CogneeError, estimate: MemIngestEstimate | None = None) -> MemError:
    hint = None
    if isinstance(exc, CogneeNotConfiguredError):
        hint = NOT_CONFIGURED_HINT
    elif isinstance(exc, CogneeUnavailableError):
        hint = "cognee is unreachable or backing off; retry later or run mem_doctor"
    elif exc.status == HTTPStatus.UNAUTHORIZED:
        hint = "authentication rejected; check COGNEE_USER_EMAIL/COGNEE_USER_PASSWORD"
    return MemError(error=str(exc), hint=hint, status=exc.status, estimate=estimate)


async def search(inp: MemSearchIn, client: CogneeClient | None = None) -> MemSearchResult | MemError:
    """Query the durable memory graph."""
    try:
        results = await _client(client).search(inp.query, inp.search_type, inp.dataset, inp.top_k)
    except CogneeError as exc:
        return _error(exc)
    return MemSearchResult(results=results, search_type=inp.search_type, dataset=inp.dataset)


async def rules(query: str, top_k: int = 10, client: CogneeClient | None = None) -> MemSearchResult | MemError:
    """Retrieve coding rules distilled into the graph (thin CODING_RULES search)."""
    return await search(MemSearchIn(query=query, search_type="CODING_RULES", top_k=top_k), client=client)


async def remember(inp: MemRememberIn, client: CogneeClient | None = None) -> MemRememberResult | MemError:
    """Store one fact: fast ``/add``, then background ``/cognify`` — never blocks on extraction."""
    text = inp.text
    if inp.metadata:
        pairs = ", ".join(f"{k}={v}" for k, v in inp.metadata.items())
        text = f"{text}\n\n[metadata: {pairs}]"
    c = _client(client)
    try:
        added = await c.add([text], inp.dataset, inp.node_set, run_in_background=False)
        await c.cognify(inp.dataset, run_in_background=True)
    except CogneeError as exc:
        return _error(exc)
    add_id = added.get("id") if isinstance(added, dict) else None
    return MemRememberResult(dataset=inp.dataset, add_id=str(add_id) if add_id else None)


def _estimate(items: list[str]) -> MemIngestEstimate:
    tokens = sum(len(t) for t in items) // _CHARS_PER_TOKEN
    usd_per_mtok = float(os.environ.get(_USD_PER_MTOK_ENV, _DEFAULT_USD_PER_MTOK))
    return MemIngestEstimate(
        items=len(items),
        estimated_tokens=tokens,
        estimated_chunks=max(1, math.ceil(tokens / _CHUNK_TOKENS)),
        estimated_cost_usd=round(tokens / 1_000_000 * usd_per_mtok, 4),
    )


async def ingest(inp: MemIngestIn, client: CogneeClient | None = None) -> MemIngestResult | MemError:
    """Bulk-ingest curated items with a cost pre-flight and serial-first cognify.

    The first document on a *fresh* dataset is cognified alone and awaited
    (``dataPerBatch=1, chunksPerBatch=1``) — dodging the CREATE TABLE graph_node pg_type
    race cognee hits when a brand-new dataset fans out — then the rest ship in one batch.
    """
    if not inp.items:
        return MemError(error="no items to ingest")
    estimate = _estimate(inp.items)
    if inp.dry_run:
        return MemIngestResult(dataset=inp.dataset, estimate=estimate, dry_run=True)
    limit = float(os.environ.get(_COST_LIMIT_ENV, _DEFAULT_COST_LIMIT_USD))
    if estimate.estimated_cost_usd > limit and not inp.confirm:
        return MemError(
            error=f"ingest refused: estimated cost ${estimate.estimated_cost_usd} exceeds the ${limit} limit",
            hint=f"re-run with confirm=true to accept, or raise {_COST_LIMIT_ENV}",
            estimate=estimate,
        )
    c = _client(client)
    try:
        fresh = await _ship(c, inp.items, inp.dataset, inp.node_set)
    except CogneeError as exc:
        return _error(exc, estimate=estimate)
    return MemIngestResult(
        dataset=inp.dataset,
        estimate=estimate,
        ingested=len(inp.items),
        fresh_dataset=fresh,
        serial_first=fresh,
    )


async def _ship(c: CogneeClient, items: list[str], dataset: str, node_set: list[str] | None) -> bool:
    """Add + cognify ``items``; serial-first on a fresh dataset. Returns whether it was fresh."""
    existing = {d.get("name") for d in await c.datasets()}
    fresh = dataset not in existing
    rest = items
    if fresh:
        await c.add(items[:1], dataset, node_set, run_in_background=False)
        await c.cognify(dataset, run_in_background=False, data_per_batch=1, chunks_per_batch=1)
        rest = items[1:]
    if rest:
        await c.add(rest, dataset, node_set, run_in_background=False)
        await c.cognify(dataset, run_in_background=True)
    return fresh


async def stats(inp: MemStatsIn, client: CogneeClient | None = None) -> MemStatsResult | MemError:
    """Best-effort dataset stats — honest about what the server cannot report.

    cognee exposes no node/edge census endpoint, so this returns dataset existence and
    pipeline status with ``node_counts_supported=False`` rather than faking authority.
    """
    c = _client(client)
    try:
        datasets = await c.datasets()
        entry = next((d for d in datasets if d.get("name") == inp.dataset), None)
        if entry is None:
            return MemError(
                error=f"dataset {inp.dataset!r} not found",
                available=sorted(str(d.get("name")) for d in datasets),
            )
        status = await c.dataset_status(inp.dataset)
    except CogneeError as exc:
        return _error(exc)
    return MemStatsResult(dataset=inp.dataset, dataset_id=str(entry.get("id")), status=status)


_ONTOLOGY_NS = "http://repo-agent-harness/ontology#"


def _sanitize(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name.strip())


def ontology_document(individuals: dict[str, str]) -> str:
    """Render an OWL RDF/XML document of ``owl:NamedIndividual`` declarations ONLY.

    cognee's ontology resolver matches individuals (fuzzy 0.8) and never matches
    ``owl:Class``, so class declarations would be dead weight — the type URIs appear only
    as ``rdf:type`` references on the individuals.
    """
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"'
        f' xmlns:owl="http://www.w3.org/2002/07/owl#"'
        f' xmlns="{_ONTOLOGY_NS}">',
    ]
    for name, type_ in sorted(individuals.items()):
        about = quoteattr(_ONTOLOGY_NS + _sanitize(name))
        resource = quoteattr(_ONTOLOGY_NS + _sanitize(type_))
        lines.extend(
            (
                f"  <owl:NamedIndividual rdf:about={about}>",
                f"    <rdf:type rdf:resource={resource}/>",
                f"  </owl:NamedIndividual>  <!-- {escape(name)}: {escape(type_)} -->",
            )
        )
    lines.append("</rdf:RDF>")
    return "\n".join(lines) + "\n"


def ontology_prompt(individuals: dict[str, str]) -> str:
    """The extraction prompt paired with the document — generated from the SAME dict.

    One source dict produces both artifacts so the type vocabulary in the prompt can never
    drift from the types in the uploaded ontology.
    """
    types = sorted(set(individuals.values()))
    return (
        "When typing extracted entities, the type must be EXACTLY ONE of: "
        + ", ".join(types)
        + ". Never invent a new type."
    )


async def ontology(inp: MemOntologyIn, client: CogneeClient | None = None) -> MemOntologyResult | MemError:
    """Generate + idempotently upload a NamedIndividual ontology; return the paired prompt.

    The upload key is a content hash, so re-running with the same dict is a no-op and any
    edit produces a new key (the server keeps both; cognify selects by ``ontologyKey``).
    """
    if not inp.individuals:
        return MemError(error="no individuals provided", hint="pass {name: type, ...}")
    xml = ontology_document(inp.individuals)
    key = "harness-" + hashlib.sha256(xml.encode("utf-8")).hexdigest()[:16]
    c = _client(client)
    try:
        uploaded = False
        if not await c.ontology_exists(key):
            await c.upload_ontology(key, xml, description="repo-agent-harness mem_ontology")
            uploaded = True
    except CogneeError as exc:
        return _error(exc)
    return MemOntologyResult(
        ontology_key=key,
        uploaded=uploaded,
        individuals=len(inp.individuals),
        types=sorted(set(inp.individuals.values())),
        prompt=ontology_prompt(inp.individuals),
    )


async def migrate_serena_memories(
    root: str,
    dataset: str = DEFAULT_DATASET,
    *,
    dry_run: bool = False,
    confirm: bool = False,
    client: CogneeClient | None = None,
) -> MemMigrateResult | MemError:
    """One-shot: ship ``.serena/memories/*.md`` into cognee; originals stay in place.

    The retirement move for Serena's memory tools — after this, cognee is the only durable
    memory surface (Serena keeps reading its own files internally for the onboarding gate,
    they just stop being an agent-facing store). Tagged ``project_docs`` plus a per-repo
    tag so the notes stay filterable by origin.
    """
    rootp = Path(root).resolve()
    mem_dir = rootp / ".serena" / "memories"
    files = sorted(p for p in mem_dir.glob("*.md")) if mem_dir.is_dir() else []
    node_set = ["project_docs", f"repo:{rootp.name}"]
    if not files:
        return MemMigrateResult(migrated=0, files=[], dataset=dataset, node_set=node_set, dry_run=dry_run)
    items = [f"# Serena memory: {p.stem} (repo {rootp.name})\n\n{p.read_text(encoding='utf-8')}" for p in files]
    out = await ingest(
        MemIngestIn(items=items, dataset=dataset, node_set=node_set, dry_run=dry_run, confirm=confirm),
        client=client,
    )
    if isinstance(out, MemError):
        return out
    return MemMigrateResult(
        migrated=0 if out.dry_run else out.ingested,
        files=[p.name for p in files],
        dataset=dataset,
        node_set=node_set,
        dry_run=out.dry_run,
        estimate=out.estimate,
    )


def _capture_sentinels() -> list[str]:
    """Detect live competing capture pipelines (claude-mem / cognee-memory plugin)."""
    hints: list[str] = []
    db = _CLAUDE_MEM_DB.expanduser()
    try:
        if db.exists() and time.time() - db.stat().st_mtime < _SENTINEL_RECENT_S:
            hints.append(
                "claude-mem capture looks LIVE (~/.claude-mem/claude-mem.db written recently) — "
                "disable it or memory is double-written"
            )
    except OSError:
        pass
    plugin_dir = _COGNEE_PLUGIN_DIR.expanduser()
    try:
        if plugin_dir.is_dir() and any(any(p.iterdir()) for p in plugin_dir.rglob("pending") if p.is_dir()):
            hints.append(
                "cognee-memory plugin has pending captures (~/.cognee-plugin/**/pending non-empty) — "
                "disable it or memory is double-written"
            )
    except OSError:
        pass
    return hints


async def doctor(client: CogneeClient | None = None) -> MemDoctorResult:
    """Checkable health: reachability, auth, and competing-capture sentinels."""
    c = _client(client)
    out = MemDoctorResult(configured=c.configured)
    if not c.configured:
        out.hints.append(NOT_CONFIGURED_HINT)
        out.hints.extend(_capture_sentinels())
        return out
    try:
        await c.health()
        out.reachable = True
    except CogneeError as exc:
        out.hints.append(f"health probe failed: {exc}")
    if out.reachable:
        try:
            datasets = await c.datasets()
            out.authenticated = True
            out.datasets = sorted(str(d.get("name")) for d in datasets)
        except CogneeError as exc:
            out.hints.append(f"authenticated probe failed: {exc}")
    out.hints.extend(_capture_sentinels())
    return out
