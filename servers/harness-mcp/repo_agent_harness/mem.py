"""Business logic for the ``mem_*`` durable-memory tools (remote cognee).

Every function returns a plain dict (the repo_* tool convention): client failures are
mapped to ``{"error", "hint", ...}`` instead of raising, so the MCP bindings in server.py
stay one-liners and the agent always gets an actionable message.
"""

from __future__ import annotations

import hashlib
import math
import os
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from repo_agent_harness import cognee_client
from repo_agent_harness.cognee_client import (
    NOT_CONFIGURED_HINT,
    CogneeClient,
    CogneeError,
    CogneeNotConfiguredError,
    CogneeUnavailableError,
)

SEARCH_TYPES = ("GRAPH_COMPLETION", "CHUNKS", "TEMPORAL", "CODING_RULES")

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


def _error(exc: CogneeError) -> dict:
    out: dict[str, Any] = {"error": str(exc)}
    if isinstance(exc, CogneeNotConfiguredError):
        out["hint"] = NOT_CONFIGURED_HINT
    elif isinstance(exc, CogneeUnavailableError):
        out["hint"] = "cognee is unreachable or backing off; retry later or run mem_doctor"
    elif exc.status == HTTPStatus.UNAUTHORIZED:
        out["hint"] = "authentication rejected; check COGNEE_USER_EMAIL/COGNEE_USER_PASSWORD"
    if exc.status is not None:
        out["status"] = exc.status
    return out


async def search(
    query: str,
    search_type: str = "GRAPH_COMPLETION",
    dataset: str | None = None,
    top_k: int = 10,
    client: CogneeClient | None = None,
) -> dict:
    """Query the durable memory graph. Returns ``{results, search_type, dataset}``."""
    if search_type not in SEARCH_TYPES:
        return {
            "error": f"unsupported search_type {search_type!r}",
            "hint": f"one of: {', '.join(SEARCH_TYPES)}",
        }
    try:
        results = await _client(client).search(query, search_type, dataset, top_k)
    except CogneeError as exc:
        return _error(exc)
    return {"results": results, "search_type": search_type, "dataset": dataset}


async def rules(
    query: str,
    top_k: int = 10,
    client: CogneeClient | None = None,
) -> dict:
    """Retrieve coding rules distilled into the graph (thin CODING_RULES search)."""
    return await search(query, search_type="CODING_RULES", top_k=top_k, client=client)


async def remember(
    text: str,
    dataset: str = DEFAULT_DATASET,
    node_set: list[str] | None = None,
    metadata: dict | None = None,
    client: CogneeClient | None = None,
) -> dict:
    """Store one fact: fast ``/add``, then background ``/cognify`` — never blocks on extraction."""
    if metadata:
        pairs = ", ".join(f"{k}={v}" for k, v in metadata.items())
        text = f"{text}\n\n[metadata: {pairs}]"
    c = _client(client)
    try:
        added = await c.add([text], dataset, node_set, run_in_background=False)
        await c.cognify(dataset, run_in_background=True)
    except CogneeError as exc:
        return _error(exc)
    add_id = added.get("id") if isinstance(added, dict) else None
    return {"queued": True, "dataset": dataset, "add_id": add_id}


def _estimate(items: list[str]) -> dict:
    tokens = sum(len(t) for t in items) // _CHARS_PER_TOKEN
    chunks = max(1, math.ceil(tokens / _CHUNK_TOKENS))
    usd_per_mtok = float(os.environ.get(_USD_PER_MTOK_ENV, _DEFAULT_USD_PER_MTOK))
    cost = tokens / 1_000_000 * usd_per_mtok
    return {
        "items": len(items),
        "estimated_tokens": tokens,
        "estimated_chunks": chunks,
        "estimated_cost_usd": round(cost, 4),
    }


async def ingest(  # noqa: PLR0913 - the tool contract: two positional inputs, keyword-only switches
    items: list[str],
    dataset: str,
    *,
    node_set: list[str] | None = None,
    dry_run: bool = False,
    confirm: bool = False,
    client: CogneeClient | None = None,
) -> dict:
    """Bulk-ingest curated items with a cost pre-flight and serial-first cognify.

    The first document on a *fresh* dataset is cognified alone and awaited
    (``dataPerBatch=1, chunksPerBatch=1``) — dodging the CREATE TABLE graph_node pg_type
    race cognee hits when a brand-new dataset fans out — then the rest ship in one batch.
    """
    if not items:
        return {"error": "no items to ingest"}
    estimate = _estimate(items)
    if dry_run:
        return {"dry_run": True, "dataset": dataset, **estimate}
    limit = float(os.environ.get(_COST_LIMIT_ENV, _DEFAULT_COST_LIMIT_USD))
    if estimate["estimated_cost_usd"] > limit and not confirm:
        return {
            "error": (f"ingest refused: estimated cost ${estimate['estimated_cost_usd']} exceeds the ${limit} limit"),
            "hint": f"re-run with confirm=true to accept, or raise {_COST_LIMIT_ENV}",
            **estimate,
        }
    c = _client(client)
    try:
        fresh = await _ship(c, items, dataset, node_set)
    except CogneeError as exc:
        return _error(exc)
    return {
        "ingested": len(items),
        "dataset": dataset,
        "fresh_dataset": fresh,
        "serial_first": fresh,
        **estimate,
    }


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


async def stats(dataset: str, client: CogneeClient | None = None) -> dict:
    """Best-effort dataset stats — honest about what the server cannot report.

    cognee exposes no node/edge census endpoint, so this returns dataset existence and
    pipeline status plus an explicit not-supported marker for the graph counts rather
    than faking authority.
    """
    c = _client(client)
    try:
        datasets = await c.datasets()
        entry = next((d for d in datasets if d.get("name") == dataset), None)
        if entry is None:
            return {
                "error": f"dataset {dataset!r} not found",
                "available": sorted(str(d.get("name")) for d in datasets),
            }
        status = await c.dataset_status(dataset)
    except CogneeError as exc:
        return _error(exc)
    return {
        "dataset": dataset,
        "dataset_id": entry.get("id"),
        "status": status,
        "node_counts_by_type": {
            "error": "not supported",
            "hint": "cognee exposes no census endpoint; graph counts unavailable upstream",
        },
    }


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


async def ontology(
    individuals: dict[str, str],
    client: CogneeClient | None = None,
) -> dict:
    """Generate + idempotently upload a NamedIndividual ontology; return the paired prompt.

    The upload key is a content hash, so re-running with the same dict is a no-op and any
    edit produces a new key (the server keeps both; cognify selects by ``ontologyKey``).
    """
    if not individuals:
        return {"error": "no individuals provided", "hint": "pass {name: type, ...}"}
    xml = ontology_document(individuals)
    key = "harness-" + hashlib.sha256(xml.encode("utf-8")).hexdigest()[:16]
    c = _client(client)
    try:
        uploaded = False
        if not await c.ontology_exists(key):
            await c.upload_ontology(key, xml, description="repo-agent-harness mem_ontology")
            uploaded = True
    except CogneeError as exc:
        return _error(exc)
    return {
        "ontology_key": key,
        "uploaded": uploaded,
        "individuals": len(individuals),
        "types": sorted(set(individuals.values())),
        "prompt": ontology_prompt(individuals),
    }


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


async def doctor(client: CogneeClient | None = None) -> dict:
    """Checkable health: reachability, auth, and competing-capture sentinels."""
    c = _client(client)
    out: dict[str, Any] = {
        "configured": c.configured,
        "reachable": False,
        "authenticated": False,
        "hints": [],
    }
    if not c.configured:
        out["hints"].append(NOT_CONFIGURED_HINT)
        out["hints"].extend(_capture_sentinels())
        return out
    try:
        await c.health()
        out["reachable"] = True
    except CogneeError as exc:
        out["hints"].append(f"health probe failed: {exc}")
    if out["reachable"]:
        try:
            datasets = await c.datasets()
            out["authenticated"] = True
            out["datasets"] = sorted(str(d.get("name")) for d in datasets)
        except CogneeError as exc:
            out["hints"].append(f"authenticated probe failed: {exc}")
    out["hints"].extend(_capture_sentinels())
    return out
