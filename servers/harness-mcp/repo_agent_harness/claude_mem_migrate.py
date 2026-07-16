"""One-shot, resumable import of the frozen claude-mem SQLite store into cognee.

The claude-mem plugin is retired; its ``~/.claude-mem/claude-mem.db`` holds the observation
and session-summary history that predates the cognee cutover. This module reads that store
STRICTLY read-only (sqlite ``mode=ro&immutable=1``) and ships curated documents through
``mem.ingest`` — dry-run by default, one central cost pre-flight before any batch, and an
idempotency ledger under ``~/.harness/migrations/`` so an interrupted run resumes instead of
double-writing. Fail behavior is deliberate: selection/preparation errors return
:class:`MemError` (mirroring the mem_* contract), never raise.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from repo_agent_harness import digest_providers, mem, paths
from repo_agent_harness.models import ClaudeMemMigrateResult, MemError, MemIngestIn

if TYPE_CHECKING:
    from collections.abc import Iterable

    from repo_agent_harness.cognee_client import CogneeClient

DEFAULT_DB = Path("~/.claude-mem/claude-mem.db")
GRANULARITIES = ("summaries-only", "digest", "raw")

_FALLBACK_DOC_CHARS = 4000  # bound for the no-LLM titles+facts concat doc

# Epochs at/above this are milliseconds: 1e11 s is the year 5138 (never a real capture),
# 1e11 ms is 1973 — so the threshold cleanly separates the two units for plausible data.
_MS_EPOCH_THRESHOLD = 100_000_000_000


def normalize_epoch_s(epoch: float | int | None) -> float | None:
    """Normalize an s- or ms-resolution epoch to seconds (claude-mem stores ms, but verify).

    Args:
        epoch: Epoch in seconds or milliseconds, or None.

    Returns:
        Epoch seconds as float, or None when the input was None.
    """
    if epoch is None:
        return None
    value = float(epoch)
    return value / 1000.0 if abs(value) >= _MS_EPOCH_THRESHOLD else value


def _iso_utc(epoch: float | int | None) -> str:
    """Render an s/ms epoch as UTC ISO-8601 (``unknown`` when absent)."""
    seconds = normalize_epoch_s(epoch)
    if seconds is None:
        return "unknown"
    return datetime.fromtimestamp(seconds, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------------------------------------------ the ledger
# Generic shipped-unit ledger (JSONL: {"hash": ..., "ts": ...} per line). Kept content-hash
# based and shape-agnostic on purpose — the later code_map migration reuses this pattern.


def ledger_read(path: Path) -> set[str]:
    """Content hashes already shipped; corrupt/torn lines are skipped, not fatal.

    Args:
        path: Ledger file (JSON-lines).

    Returns:
        The set of ``hash`` values from every parseable line.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    done: set[str] = set()
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # a torn line must not void the rest of the ledger (resume > strictness)
        if isinstance(entry, dict) and isinstance(entry.get("hash"), str):
            done.add(entry["hash"])
    return done


def ledger_append(path: Path, hashes: Iterable[str]) -> None:
    """Append shipped-unit hashes batch-wise, atomically (tmp + os.replace).

    Same atomicity pattern as :func:`paths.stamp_hook_heartbeat`: readers never see a torn
    file; a crash between batches loses at most the un-appended batch, which simply
    re-ships on resume (ingest is idempotent enough for that trade).

    Args:
        path: Ledger file (created, parents included, on first append).
        hashes: Content hashes of the units that just shipped.
    """
    entries = list(hashes)
    if not entries:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        prior = path.read_text(encoding="utf-8")
    except OSError:
        prior = ""
    now = time.time()
    lines = "".join(json.dumps({"hash": h, "ts": now}) + "\n" for h in entries)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(prior + lines, encoding="utf-8")
    tmp.replace(path)


def ledger_path(dataset: str) -> Path:
    """The per-dataset migration ledger under ``harness_home()/migrations``."""
    return paths.harness_home() / "migrations" / f"claude-mem-{dataset}.json"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -------------------------------------------------------------- store reading
# Column lists verified against the real store's sqlite_master (2026-07-16); the
# user_prompts and sdk_sessions tables are deliberately never read.

_OBS_SQL = (
    "SELECT id, memory_session_id, project, type, title, subtitle, facts, narrative,"
    " concepts, files_read, files_modified, text, created_at_epoch FROM observations"
)
_SUM_SQL = (
    "SELECT id, memory_session_id, project, request, investigated, learned, completed,"
    " next_steps, files_read, files_edited, notes, created_at_epoch FROM session_summaries"
)


def _load_rows(db: Path) -> tuple[list[dict], list[dict]]:
    """Read observations + session summaries strictly read-only (may raise sqlite3.Error).

    ``mode=ro&immutable=1`` guarantees sqlite never writes — no journal, no WAL, no lock
    files — so the frozen store's bytes are provably untouched.
    """
    con = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
    try:
        con.row_factory = sqlite3.Row
        observations = [dict(r) for r in con.execute(_OBS_SQL)]
        summaries = [dict(r) for r in con.execute(_SUM_SQL)]
    finally:
        con.close()
    return observations, summaries


def _parse_when(value: str | None) -> float | None:
    """ISO-8601 date/datetime or a raw s/ms epoch -> epoch seconds (raises ValueError)."""
    if not value:
        return None
    try:
        numeric = float(value)
    except ValueError:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=UTC)  # naive filter input means UTC — the store's epochs are UTC
        return dt.timestamp()
    return normalize_epoch_s(numeric)


def _keep(
    row: dict, projects: list[str] | None, types: list[str] | None, window: tuple[float | None, float | None]
) -> bool:
    """The selection predicate: project/type membership plus the since/until window."""
    if projects and row.get("project") not in projects:
        return False
    if types and row.get("type") not in types:
        return False
    since_s, until_s = window
    stamp = normalize_epoch_s(row.get("created_at_epoch"))
    if since_s is not None and (stamp is None or stamp < since_s):
        return False
    return not (until_s is not None and (stamp is None or stamp > until_s))


# --------------------------------------------------------------- doc rendering


def _json_list(raw: object) -> list[str]:
    """A claude-mem JSON-array column as list[str]; malformed content stays visible."""
    if not raw:
        return []
    try:
        value = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return [str(raw)]  # defensive: surface the malformed cell instead of dropping it
    if isinstance(value, list):
        return [str(v) for v in value if str(v)]
    return [str(value)]


def _dedup(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _doc_header(heading: str, project: str | None, epoch: float | int | None) -> list[str]:
    """The shared doc preamble: H1 plus the Project/Observed provenance lines."""
    return [f"# claude-mem {heading}", "", f"Project: {project or 'unknown'}", f"Observed: {_iso_utc(epoch)}", ""]


def _doc_trailer(source: str, session: object, row_id: object = None) -> list[str]:
    """The shared metadata trailer (same bracket style as mem.remember's metadata)."""
    ref = f" id={row_id}" if row_id is not None else ""
    return ["", f"[claude-mem: source={source}{ref} session={session}]"]


def _render_observation(row: dict) -> str:
    """One observation row -> one curated markdown doc (header, facts, narrative, tags)."""
    title = (row.get("title") or "").strip() or "untitled"
    heading = f"{row.get('type') or 'observation'}: {title}"
    lines = _doc_header(heading, row.get("project"), row.get("created_at_epoch"))
    subtitle = (row.get("subtitle") or "").strip()
    if subtitle:
        lines += [subtitle, ""]
    facts = _json_list(row.get("facts"))
    if facts:
        lines += [f"- {fact}" for fact in facts]
        lines.append("")
    narrative = (row.get("narrative") or row.get("text") or "").strip()
    if narrative:
        lines += [narrative, ""]
    concepts = _json_list(row.get("concepts"))
    if concepts:
        lines.append(f"Concepts: {', '.join(concepts)}")
    files = _dedup(_json_list(row.get("files_read")) + _json_list(row.get("files_modified")))
    if files:
        lines.append(f"Files: {', '.join(files)}")
    lines += _doc_trailer("observation", row.get("memory_session_id"), row.get("id"))
    return "\n".join(lines)


_SUMMARY_SECTIONS = (
    ("request", "Request"),
    ("investigated", "Investigated"),
    ("learned", "Learned"),
    ("completed", "Completed"),
    ("next_steps", "Next steps"),
    ("notes", "Notes"),
)


def _render_summary(row: dict) -> str:
    """One session-summary row -> one curated markdown doc (non-empty sections only)."""
    request = (row.get("request") or "").strip()
    title = request or f"session {str(row.get('memory_session_id') or '')[:8]}"
    lines = _doc_header(f"session summary: {title[:120]}", row.get("project"), row.get("created_at_epoch"))
    for key, label in _SUMMARY_SECTIONS:
        value = (row.get(key) or "").strip()
        if value:
            lines.append(f"{label}: {value}")
    files = _dedup(_json_list(row.get("files_read")) + _json_list(row.get("files_edited")))
    if files:
        lines.append(f"Files: {', '.join(files)}")
    lines += _doc_trailer("session_summary", row.get("memory_session_id"), row.get("id"))
    return "\n".join(lines)


# ---------------------------------------------------------- idempotency units


@dataclass
class _Unit:
    """One idempotency unit: a single doc (raw/summary) or a (project, session) digest group."""

    project: str
    content_hash: str = ""
    docs: list[str] | None = None  # deterministic docs; None = digest group, distilled at ship time
    session: str = ""
    summary: dict | None = None
    observations: list[dict] = field(default_factory=list)

    def member_rows(self) -> list[dict]:
        """Every source row backing this unit (observations first, then the summary)."""
        return [*self.observations, *([self.summary] if self.summary is not None else [])]

    def latest_epoch(self) -> float | None:
        """The group's most recent normalized timestamp (its 'Observed' stamp)."""
        stamps = [normalize_epoch_s(r.get("created_at_epoch")) for r in self.member_rows()]
        known = [s for s in stamps if s is not None]
        return max(known) if known else None

    def fallback_docs(self) -> list[str]:
        """The no-LLM rendering: summary when present, else bounded titles+facts concat."""
        if self.summary is not None:
            return [_render_summary(self.summary)]
        return [_fallback_concat(self)]

    def estimate_docs(self) -> list[str]:
        """The deterministic texts a dry-run estimate is based on (never an LLM call)."""
        return self.docs if self.docs is not None else self.fallback_docs()


def _fallback_concat(unit: _Unit) -> str:
    """Digest substitute when no provider/summary exists — a session is never dropped."""
    lines = _doc_header(f"session digest: {unit.project} session {unit.session[:8]}", unit.project, unit.latest_epoch())
    for row in unit.observations:
        title = (row.get("title") or "").strip() or "untitled"
        lines.append(f"- [{row.get('type') or 'observation'}] {title}")
        lines += [f"  - {fact}" for fact in _json_list(row.get("facts"))]
    lines += _doc_trailer("session_fallback", unit.session)
    return "\n".join(lines)[:_FALLBACK_DOC_CHARS]


def _doc_unit(project: str, doc: str) -> _Unit:
    return _Unit(project=project, content_hash=_sha256(doc), docs=[doc])


def _digest_units(observations: list[dict], summaries: list[dict]) -> list[_Unit]:
    """Group rows by (project, session); the group hash is the sha256 of sorted member hashes.

    Hashing the deterministic member renders (not the LLM output) keeps re-runs stable
    despite digest nondeterminism.
    """
    groups: dict[tuple[str, str], _Unit] = {}

    def group(row: dict) -> _Unit:
        key = (str(row.get("project")), str(row.get("memory_session_id")))
        return groups.setdefault(key, _Unit(project=key[0], session=key[1]))

    for row in observations:
        group(row).observations.append(row)
    for row in summaries:
        group(row).summary = row
    for unit in groups.values():
        members = [_sha256(_render_observation(r)) for r in unit.observations]
        if unit.summary is not None:
            members.append(_sha256(_render_summary(unit.summary)))
        unit.content_hash = _sha256("\n".join(sorted(members)))
    return list(groups.values())


def _build_units(granularity: str, observations: list[dict], summaries: list[dict]) -> list[_Unit]:
    if granularity == "raw":
        return [_doc_unit(str(row.get("project")), _render_observation(row)) for row in observations]
    if granularity == "summaries-only":
        return [_doc_unit(str(row.get("project")), _render_summary(row)) for row in summaries]
    return _digest_units(observations, summaries)


async def _materialize(unit: _Unit) -> list[str]:
    """The unit's final docs; digest groups distill at ship time, failing open to fallback."""
    if unit.docs is not None:
        return unit.docs
    entries = unit.fallback_docs() + [_render_observation(r) for r in unit.observations]
    try:
        reply = await digest_providers.digest(entries)
    except Exception:  # noqa: BLE001 - digest is best-effort (capture pipeline contract); the fallback keeps the session
        reply = None
    if reply is None:
        return unit.fallback_docs()
    if reply.observations:
        return [_render_digest_observation(obs, unit) for obs in reply.observations]
    if reply.text and reply.text.strip():
        return [_render_digest_text(reply.text.strip(), unit)]
    return unit.fallback_docs()


def _render_digest_observation(obs: digest_providers.DigestObservation, unit: _Unit) -> str:
    """One distilled DigestObservation -> the same doc shape as a raw observation."""
    lines = _doc_header(f"{obs.type}: {obs.title}", unit.project, unit.latest_epoch())
    lines += [f"- {fact}" for fact in obs.facts]
    if obs.concepts:
        lines.append(f"Concepts: {', '.join(obs.concepts)}")
    if obs.files:
        lines.append(f"Files: {', '.join(obs.files)}")
    lines += _doc_trailer("session_digest", unit.session)
    return "\n".join(lines)


def _render_digest_text(text: str, unit: _Unit) -> str:
    """A plaintext digest reply -> one doc carrying the prose verbatim."""
    lines = _doc_header(f"session digest: {unit.project} session {unit.session[:8]}", unit.project, unit.latest_epoch())
    lines += [text]
    lines += _doc_trailer("session_digest", unit.session)
    return "\n".join(lines)


# -------------------------------------------------------------------- migrate


def _per_type(observations: list[dict], summaries: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in observations:
        key = str(row.get("type") or "unknown")
        counts[key] = counts.get(key, 0) + 1
    if summaries:
        counts["session_summary"] = len(summaries)
    return counts


def _split_pending(units: list[_Unit], done: set[str]) -> tuple[list[_Unit], int]:
    """Drop units already in the ledger (and in-run duplicates); count what was skipped."""
    seen: set[str] = set()
    pending: list[_Unit] = []
    skipped = 0
    for unit in units:
        if unit.content_hash in done or unit.content_hash in seen:
            skipped += 1
            continue
        seen.add(unit.content_hash)
        pending.append(unit)
    return pending, skipped


async def _execute(
    pending: list[_Unit],
    dataset: str,
    base_tags: list[str],
    result: ClaudeMemMigrateResult,
    client: CogneeClient | None,
) -> ClaudeMemMigrateResult | MemError:
    """Ship per-project batches through mem.ingest; ledger-append after each success.

    A mid-run failure returns the MemError as-is: every batch that already shipped is in
    the ledger, so the next run resumes exactly where this one stopped.
    """
    ledger = ledger_path(dataset)
    result.dry_run = False
    result.per_project = {}
    by_project: dict[str, list[_Unit]] = {}
    for unit in pending:
        by_project.setdefault(unit.project, []).append(unit)
    for project in sorted(by_project):
        units = by_project[project]
        docs = [doc for unit in units for doc in await _materialize(unit)]
        tags = [base_tags[0], f"{mem.PROJECT_TAG_PREFIX}{project}", *base_tags[1:]]
        out = await mem.ingest(
            MemIngestIn(items=docs, dataset=dataset, node_set=tags, dry_run=False, confirm=True),
            client=client,
        )
        if isinstance(out, MemError):
            return out
        ledger_append(ledger, [unit.content_hash for unit in units])
        result.per_project[project] = len(docs)
        result.shipped += len(docs)
    return result


async def migrate(  # noqa: PLR0913, PLR0911 - the filter surface mirrors the CLI (plain args per the serena-migration template); every early return is a distinct refusal
    db_path: str | Path | None = None,
    dataset: str = mem.DEFAULT_DATASET,
    *,
    node_set: list[str] | None = None,
    projects: list[str] | None = None,
    types: list[str] | None = None,
    since: str | None = None,
    until: str | None = None,
    granularity: str = "summaries-only",
    dry_run: bool = True,
    confirm: bool = False,
    client: CogneeClient | None = None,
) -> ClaudeMemMigrateResult | MemError:
    """One-shot, resumable import of the frozen claude-mem store into cognee.

    Dry-run by default: reports selection counts, pending docs, and the cost estimate
    without any cognee call. Execution requires ``confirm=True`` and passes ONE central
    cost pre-flight (COGNEE_INGEST_COST_LIMIT_USD) before any batch ships; per-project
    batches then go through ``mem.ingest(confirm=True)`` (serial-first + post-ingest
    memify) and land in the idempotency ledger.

    Args:
        db_path: The claude-mem sqlite store (default ``~/.claude-mem/claude-mem.db``).
        dataset: Target cognee dataset (also keys the idempotency ledger).
        node_set: Extra node_set tags appended after the import + project tags.
        projects: Only these claude-mem projects.
        types: Only these observation types (raw/digest granularity).
        since: Only rows observed at/after this ISO-8601 date/datetime or epoch.
        until: Only rows observed at/before this ISO-8601 date/datetime or epoch.
        granularity: ``summaries-only`` (default) | ``digest`` | ``raw``.
        dry_run: Only report; never touches cognee. The default.
        confirm: Required for execution — without it the run stays a refusal.
        client: Injected cognee client (tests); defaults to the configured one.

    Returns:
        ClaudeMemMigrateResult report, or MemError on refusal/failure.
    """
    if granularity not in GRANULARITIES:
        return MemError(error=f"unknown granularity {granularity!r}", hint=f"one of: {', '.join(GRANULARITIES)}")
    db = Path(db_path).expanduser() if db_path else DEFAULT_DB.expanduser()
    if not db.is_file():
        return MemError(error=f"claude-mem store not found: {db}")
    try:
        window = (_parse_when(since), _parse_when(until))
    except ValueError:
        return MemError(error=f"unparseable since/until filter: {since!r}/{until!r}", hint="use ISO-8601 or an epoch")
    try:
        observations, summaries = _load_rows(db)
    except sqlite3.Error as exc:
        return MemError(error=f"claude-mem store unreadable: {exc}", hint=f"is {db} really the claude-mem sqlite db?")
    if granularity == "raw":
        summaries = []
    elif granularity == "summaries-only":
        observations = []
    observations = [r for r in observations if _keep(r, projects, types, window)]
    summaries = [r for r in summaries if _keep(r, projects, None, window)]
    units = _build_units(granularity, observations, summaries)
    pending, skipped = _split_pending(units, ledger_read(ledger_path(dataset)))
    estimate_texts = [doc for unit in pending for doc in unit.estimate_docs()]
    estimate = mem._estimate(estimate_texts)  # noqa: SLF001 - blessed shared pre-flight, same table as mem.ingest
    result = ClaudeMemMigrateResult(
        dataset=dataset,
        granularity=granularity,
        db=str(db),
        dry_run=True,
        observations=len(observations),
        summaries=len(summaries),
        sessions=len({(str(r.get("project")), str(r.get("memory_session_id"))) for r in observations + summaries}),
        per_project=_docs_per_project(pending),
        per_type=_per_type(observations, summaries),
        estimated_docs=len(estimate_texts),
        skipped_dedup=skipped,
        node_set=[mem.NODE_SET_CLAUDE_MEM_IMPORT, *(node_set or [])],
        estimate=estimate,
    )
    if dry_run:
        return result
    if not confirm:
        return MemError(
            error="migrate-claude-mem executes only with confirm=true (dry-run is the default)",
            hint="review the dry-run report first, then re-run with --confirm",
            estimate=estimate,
        )
    limit = float(os.environ.get(mem._COST_LIMIT_ENV, mem._DEFAULT_COST_LIMIT_USD))  # noqa: SLF001 - same cost-gate table as mem.ingest
    if estimate.estimated_cost_usd > limit:
        return MemError(
            error=f"migration refused: estimated cost ${estimate.estimated_cost_usd} exceeds the ${limit} limit",
            hint=f"narrow the selection (projects/types/since) or raise {mem._COST_LIMIT_ENV}",  # noqa: SLF001
            estimate=estimate,
        )
    return await _execute(pending, dataset, result.node_set, result, client)


def _docs_per_project(pending: list[_Unit]) -> dict[str, int]:
    """Pending doc counts per project (estimate basis for digest groups)."""
    counts: dict[str, int] = {}
    for unit in pending:
        counts[unit.project] = counts.get(unit.project, 0) + len(unit.estimate_docs())
    return counts
