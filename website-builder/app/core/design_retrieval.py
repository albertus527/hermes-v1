"""Design resource retrieval — normalized, bounded, provenance-carrying reads (Batch D1).

D0 answered two questions: *is this resource configured?* and *is it available
on this machine?*. It deliberately built **no way to ask a resource for
anything**. Everything downstream — Design DNA, FRONTEND, the critic — still
had seven bespoke readers to invent, which is the exact shape that becomes the
next source of non-convergence: unbounded context dumps, silent fallbacks that
invent guidance when a resource is absent, and resource text treated as
instructions.

This module is the one narrow answer. It is a **normalization layer**: a small,
table-driven adapter set over the D0 manifest, producing one bounded, typed,
provenance-carrying result shape.

Four properties are load-bearing and each one is a bug class being prevented:

**Bounded.** Every context contribution passes through hard, deterministic
limits (:class:`DesignContextLimits`). Same input produces identical bytes --
stable request order, stable source order, no scoring, no set or dict iteration
order in the output path.

**Degrading, never inventing.** An unavailable optional resource returns zero
entries plus a degraded flag and a static warning. There is **no** fallback to
model knowledge and **no** fallback to another resource's content. Inventing
guidance to fill a gap is worse than admitting the gap, because the invented
text is indistinguishable from the real thing once it is in context.

**Data, not authority.** :class:`DesignEntry` has no field capable of carrying
an instruction into an authority position. Instruction-like text in a dataset
round-trips verbatim as an inert string, and there is no code path that
promotes it.

**Adapter policy lives in code, cross-checked against the manifest.** An adapter
may read only a locator the manifest already declares in ``data_entries``, or
``SKILL.md`` (which D0 already verifies). Naming anything else renders that
adapter *inert* -- zero entries and a static warning, never an import error and
never an unverified read. A YAML copy of the same mapping would be a drift
hazard; a second config file would dilute D0's "one source of truth" claim.
The cross-check keeps both honest.

**What this batch deliberately does NOT do.** It does not wire resources into
FRONTEND, Design DNA, the repair loop, or the agent loop. It performs no
subprocess execution: ``scripts/search.py`` is never run; retrieval reads pinned
data files in-process. It installs nothing and makes no LLM calls.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_capabilities import (
    CAPABILITY_STATUSES,
    STATUS_AVAILABLE,
    STATUS_NOT_INSTALLED,
    STATUS_UNAVAILABLE_OPTIONAL,
    STATUS_UNAVAILABLE_REQUIRED,
    DesignCapabilityReport,
    entry_is_contained,
    resolve_design_capabilities,
)
from app.core.design_resources import (
    DesignResource,
    DesignResourceManifest,
    DesignResourceManifestError,
    design_profile_skills_dir,
    load_design_resource_manifest,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result vocabulary
# ---------------------------------------------------------------------------

#: The kinds a normalized entry may carry. A closed set: an entry that does not
#: fit one of these is not something a consumer knows how to render.
ENTRY_KINDS: Tuple[str, ...] = ("guidance", "reference", "critic_finding", "policy")

#: Resource kinds that are retrieved as CONTENT. The rest are policy or registry
#: declarations and are never read for text.
RETRIEVABLE_RESOURCE_KINDS: frozenset = frozenset(
    {"skill", "reference"}
)

#: Resource kinds that exist only to be described by the dependency ladder.
#: Retrieving these as content would be a category error -- there is no
#: guidance file inside an npm package declaration to read.
POLICY_ONLY_RESOURCE_KINDS: frozenset = frozenset({"registry", "npm_optional"})

#: Static, sanitized warnings. These describe a CLASS of outcome, never a path,
#: never file content. Nothing read from the filesystem reaches a warning.
WARNING_ABSENT = "resource is not available on this host; no guidance retrieved"
WARNING_DEFERRED = "no retrieval adapter is wired for this resource kind; no guidance retrieved"
WARNING_REFERENCE_UNAVAILABLE = (
    "reference corpus has no local, verified content on this host; "
    "no guidance retrieved and none invented"
)
WARNING_CRITIC_INERT = (
    "critic adapter is inert: the resource is absent and no findings are "
    "synthesized"
)
WARNING_UNDECLARED_LOCATOR = (
    "adapter names a locator the manifest does not declare; "
    "adapter rendered inert and no file was read"
)
WARNING_TRUNCATED_ENTRIES = "entries were dropped to satisfy the per-resource entry limit"
WARNING_TRUNCATED_RESOURCE = "resource payload was truncated to satisfy the per-resource character limit"
WARNING_DROPPED_FOR_BUDGET = "further resources were skipped to satisfy the total character limit"
WARNING_RESOURCE_LIMIT = "requested resources beyond the first N were not retrieved"
WARNING_MALFORMED = "adapter output could not be parsed; no entries were produced"
WARNING_POLICY_ONLY = (
    "resource is a project-dependency declaration, not retrievable content; "
    "described by the dependency policy ladder only"
)


# ---------------------------------------------------------------------------
# Bounds
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DesignContextLimits:
    """Hard, deterministic bounds on every context contribution.

    All five are overridable and all five are enforced. They exist because a
    resource is untrusted *data of unknown size*, and an unbounded read into a
    prompt is both a context blow-up and an injection surface.
    """

    #: How many resources one report may cover. Overflow drops the remainder of
    #: the REQUESTED order and sets the report flag -- first N in requested
    #: order, so a caller that asked for four gets exactly the three it named
    #: first.
    max_resources: int = 3

    #: Entries kept per resource, in source order. Overflow drops the remainder
    #: and records ``dropped_entries``.
    max_entries_per_resource: int = 8

    #: Characters per normalized entry's ENTIRE payload -- title, body, every
    #: field key and value, and provenance. See :func:`payload_chars` for why
    #: the whole payload and not ``body`` alone.
    max_entry_chars: int = 1200

    #: Characters per resource across all its entries.
    max_resource_chars: int = 6000

    #: Characters across the whole report.
    max_total_chars: int = 16000

    def to_dict(self) -> Dict[str, int]:
        return {
            "max_resources": self.max_resources,
            "max_entries_per_resource": self.max_entries_per_resource,
            "max_entry_chars": self.max_entry_chars,
            "max_resource_chars": self.max_resource_chars,
            "max_total_chars": self.max_total_chars,
        }


DEFAULT_LIMITS = DesignContextLimits()


def payload_chars(entry: "DesignEntry") -> int:
    """Canonical size of one normalized entry.

    **The single definition** used by the per-entry, per-resource, and
    aggregate budgets alike. A second, divergent size calculation is exactly
    the bug this prevents: an aggregate that summed ``body`` lengths while the
    per-entry cap counted the full payload would under-report overflow by
    precisely the overhead it ignored, and a row with a three-character body
    and a 4 KB fields map would sail through a naive ``len(body)`` check while
    shipping a 4 KB entry.

    Provenance is counted because a later batch may place it into model
    context, and bounding a field that later reaches the model is the point.
    ``locator`` in particular originates in resource data, so it is
    attacker-shaped text.
    """
    total = len(entry.title or "") + len(entry.body or "")
    for key, value in entry.fields.items():
        total += len(key) + len(value)
    provenance = entry.provenance
    total += len(provenance.resource_id or "")
    total += len(provenance.resource_kind or "")
    total += len(provenance.adapter or "")
    total += len(provenance.locator or "")
    total += len(str(provenance.entry_index))
    return total


# ---------------------------------------------------------------------------
# Normalized result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EntryProvenance:
    """Where one entry came from.

    ``locator`` is ALWAYS skill-root-relative and never absolute. An absolute
    host path in a result would leak the operator's directory layout into
    context and into logs.
    """

    resource_id: str
    resource_kind: str
    adapter: str
    locator: str
    entry_index: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "adapter": self.adapter,
            "locator": self.locator,
            "entry_index": self.entry_index,
        }

    def to_text(self) -> str:
        """Single-line text form, counted by :func:`payload_chars`."""
        return (
            f"{self.resource_id}/{self.resource_kind}/{self.adapter}"
            f"#entry={self.entry_index}@{self.locator}"
        )


@dataclass(frozen=True)
class DesignEntry:
    """One normalized unit of design content.

    ``body`` and ``fields`` are DATA strings. There is deliberately **no**
    authority-bearing field on this type -- no ``instruction``, no
    ``requirement``, no ``override`` -- and that absence is the trust boundary.
    Instruction-like text round-trips verbatim as an inert string that no code
    path can promote into a constraint.
    """

    entry_id: str
    kind: str
    title: str
    body: str
    fields: Dict[str, str]
    provenance: EntryProvenance
    truncated: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body,
            "fields": dict(self.fields),
            "provenance": self.provenance.to_dict(),
            "truncated": self.truncated,
        }


@dataclass(frozen=True)
class CriticFinding:
    """Canonical schema for a critic finding.

    **Declared in D1; produced by no adapter in this batch.** The Impeccable
    resource is absent on this host and its real structure has not been
    inspected, so a ``SKILL.md`` -> finding parser would be an invented
    contract: it would mis-map whatever prose happens to sit in the file, and
    tests against synthetic fixtures would prove the fiction rather than the
    resource.

    The schema exists now so the shape is settled before any real parser is
    written against it. Availability is never faked.
    """

    rule_id: str
    category: str
    severity: str
    finding: str
    evidence: str
    suggested_action: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "severity": self.severity,
            "finding": self.finding,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True)
class DesignResourceResult:
    """One resource's retrieval outcome."""

    resource_id: str
    resource_kind: str
    status: str
    available: bool
    degraded: bool
    entries: Tuple[DesignEntry, ...]
    warnings: Tuple[str, ...]
    truncated: bool
    dropped_entries: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "resource_id": self.resource_id,
            "resource_kind": self.resource_kind,
            "status": self.status,
            "available": self.available,
            "degraded": self.degraded,
            "entries": [entry.to_dict() for entry in self.entries],
            "warnings": list(self.warnings),
            "truncated": self.truncated,
            "dropped_entries": self.dropped_entries,
        }

    @property
    def chars(self) -> int:
        """Total canonical payload chars across this resource's entries."""
        return sum(payload_chars(entry) for entry in self.entries)


@dataclass(frozen=True)
class DesignRetrievalReport:
    """Bounded, deterministic, serializable result of a retrieval batch."""

    ok: bool
    requested: Tuple[str, ...]
    resolved: Tuple[str, ...]
    unavailable_optional: Tuple[str, ...]
    results: Dict[str, DesignResourceResult]
    total_entries: int
    total_chars: int
    truncated: bool
    limits: Dict[str, int]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "requested": list(self.requested),
            "resolved": list(self.resolved),
            "unavailable_optional": list(self.unavailable_optional),
            "results": {
                rid: result.to_dict() for rid, result in sorted(self.results.items())
            },
            "total_entries": self.total_entries,
            "total_chars": self.total_chars,
            "truncated": self.truncated,
            "limits": dict(self.limits),
        }

    def summary(self) -> str:
        """One bounded line: ids, counts, and truncation flags.

        Never payloads, never source content, never credentials, never
        absolute paths. This is the only string that gets logged.
        """
        return (
            f"requested={len(self.requested)} "
            f"resolved={','.join(self.resolved) or '-'} "
            f"unavailable_optional={','.join(self.unavailable_optional) or '-'} "
            f"entries={self.total_entries} "
            f"chars={self.total_chars} "
            f"truncated={self.truncated}"
        )


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

#: Marker embedded in any shrunk text so a consumer can never mistake a clipped
#: value for a complete one. Without it, a silently truncated column reads as
#: the real value, which is the failure this exists to prevent.
TRUNCATION_MARKER = " [truncated]"

#: Marker embedded where a field was dropped entirely for budget.
_FIELD_DROPPED_MARKER = " [dropped]"


def _truncate_text(text: str, budget: int) -> Tuple[str, bool]:
    """Clamp ``text`` to ``budget`` chars, marking it when shortened.

    Deterministic by construction: a plain prefix cut, no ellipsis strategy
    selection, no dependence on anything but the string and the budget.
    """
    if budget <= 0:
        return "", bool(text)
    if len(text) <= budget:
        return text, False
    if budget <= len(TRUNCATION_MARKER):
        return text[:budget], True
    keep = budget - len(TRUNCATION_MARKER)
    return text[:keep] + TRUNCATION_MARKER, True


def _shrink_entry_to_budget(entry: DesignEntry, budget: int) -> Optional[DesignEntry]:
    """Shrink one entry's payload to ``budget`` chars, or drop it.

    Shrinkage order is **fixed and explicit** so the same oversized input always
    normalizes to the same bytes:

    1. **Provenance first, never dropped.** It is what makes an entry traceable
       to its source. An entry without it is unciteable, and unciteable is
       worse than absent -- a downstream reader would have no way to tell an
       authenticated resource from an unsourced string.
    2. Title, bounded deterministically.
    3. ``fields`` processed in **stable header order** (the CSV header order,
       not dict-insertion coincidence).
    4. **Field keys stay whole.** A truncated key corrupts the column identity
       and produces two indistinguishable half-columns.
    5. Field **values** truncate against the remaining budget; once exhausted,
       the remaining fields are dropped with a visible marker.
    6. Body consumes whatever budget is left.
    7. If mandatory metadata alone (provenance plus the identity fields) still
       cannot fit, **drop the entry** rather than emit an over-budget one.

    Returns ``None`` when the entry cannot fit at all -- never an over-budget
    entry, which would defeat every downstream invariant.
    """
    provenance_text = entry.provenance.to_text()
    mandatory = len(provenance_text) + len(entry.entry_id or "") + len(entry.kind or "")

    # Step 7: mandatory metadata must fit on its own.
    if mandatory > budget:
        return None

    remaining = budget - mandatory
    was_truncated = False

    # Step 2: title.
    title, cut = _truncate_text(entry.title or "", remaining)
    was_truncated = was_truncated or cut
    remaining -= len(title)

    # Steps 3-5: fields in stable header order, keys whole, values clamped.
    fields: Dict[str, str] = {}
    for key in entry.fields:
        value = entry.fields[key]
        if remaining <= 0:
            # Remaining fields are dropped rather than emitted empty.
            was_truncated = True
            continue
        if len(key) > remaining:
            # The key itself no longer fits: stop, because a truncated key
            # corrupts column identity.
            was_truncated = True
            remaining = 0
            continue
        remaining -= len(key)
        clamped, cut = _truncate_text(value, remaining)
        fields[key] = clamped
        remaining -= len(clamped)
        was_truncated = was_truncated or cut

    # Step 6: body takes whatever is left.
    body, cut = _truncate_text(entry.body or "", remaining)
    was_truncated = was_truncated or cut

    return DesignEntry(
        entry_id=entry.entry_id,
        kind=entry.kind,
        title=title,
        body=body,
        fields=fields,
        provenance=entry.provenance,
        truncated=entry.truncated or was_truncated,
    )


# ---------------------------------------------------------------------------
# Query filtering
# ---------------------------------------------------------------------------

#: Bounds on a caller-supplied query. Query text is inert data: it is never
#: compiled into a regular expression, never eval'd, and never used to build a
#: filesystem path.
MAX_QUERY_CHARS = 256

#: Split on any non-alphanumeric run. Tokens longer than this are ignored --
#: a pathological single token should not force a substring search over the
#: whole dataset.
MAX_QUERY_TOKEN_CHARS = 64

_TOKEN_SPLIT_RE = re.compile(r"[^0-9A-Za-z]+")


def normalize_query_tokens(query: Optional[str]) -> Tuple[str, ...]:
    """Split ``query`` into normalized, case-insensitive lexical tokens.

    Returns an empty tuple for an empty or whitespace-only query, which the
    adapters read as "stable source order, first N rows" -- the documented
    resting behaviour, not a silent no-op.

    This is :func:`lexical_tokens` with the query-specific character bound
    applied first, so a query and a cell are tokenized by ONE function.
    """
    if not query:
        return ()
    return lexical_tokens(query[:MAX_QUERY_CHARS])


def lexical_tokens(text: Optional[str]) -> Tuple[str, ...]:
    """Split arbitrary text into normalized, case-insensitive lexical tokens.

    **The one tokenizer.** Both the query side and the cell side call it, so a
    token compared on the way in is produced exactly the way it is compared on
    the way out. Two tokenizers would make a match depend on which side
    normalized, and the mismatch would look like a recall bug rather than a
    symmetric-tokenizer bug.

    Tokens are runs of ``[0-9A-Za-z]``; every other character is a separator.
    That single rule is what makes matching *lexical*:

    * ``"no"`` does **not** match ``"Ignore"`` -- the substring test that made
      it match (``"no" in "ig" + "no" + "re"``) was a silent recall explosion
      that surfaced every row containing the letters n-o anywhere, including
      ``Ignore``, ``Known``, and ``Font``. Recall is a quality problem; a query
      that matches a third of the corpus for no reason is a correctness one.
    * ``"3d"``, ``"ui"``, and ``"hero"`` stay intact: digits and short
      alphanumeric runs are ordinary lexical tokens, not special cases.

    Over-long tokens are dropped rather than truncated, so a single pathological
    run cannot force a match against a partial word. Splitting is bounded work
    on a bounded string; nothing here is compiled into a regular expression.
    """
    if not text:
        return ()
    tokens = []
    for raw in _TOKEN_SPLIT_RE.split(text):
        if not raw:
            continue
        token = raw.lower()
        if len(token) <= MAX_QUERY_TOKEN_CHARS:
            tokens.append(token)
    return tuple(tokens)


def row_matches_tokens(row: Mapping[str, str], tokens: Sequence[str]) -> bool:
    """True when ``row`` contains any of ``tokens`` as a **whole lexical token**.

    **Every parsed field is searched; no column is excluded by name.** An
    earlier draft proposed excluding columns "named to imply instruction"
    (``instruction``, ``prompt``, ``recommendation``), and that heuristic was
    removed for two reasons:

    * It buys no safety. The trust boundary does not rest on what a column is
      called: matched text lands in a ``fields`` **value**, and ``DesignEntry``
      has no authority-bearing field to promote it into. A cell called
      ``recommendation`` is exactly as inert as one called ``css``.
    * A name-based list breaks the moment the real schema differs from the
      guess, protecting nothing real while silently narrowing recall.

    Each cell is tokenized by :func:`lexical_tokens` -- the same function the
    query side uses -- and compared by **token equality**, never by substring.
    Matching is case-insensitive, unordered, and unscored. A row matching more
    tokens does **not** outrank one matching fewer: ranking would make output
    order depend on a scoring function, and "same input, same bytes" is the
    property this batch is selling.

    The comparison is a set intersection bounded by the cell, with no fuzzy
    matching, no edit distance, no stemming, no regular expression built from
    caller text, and no rerank of any kind.
    """
    if not tokens:
        return True
    wanted = set(tokens)
    for value in row.values():
        if not isinstance(value, str):
            continue
        if wanted.intersection(lexical_tokens(value)):
            return True
    return False


def filter_rows(
    rows: Sequence[Mapping[str, str]], tokens: Sequence[str]
) -> List[Mapping[str, str]]:
    """Select matching rows, **preserving source order**.

    Runs over the WHOLE pinned dataset before any count cap applies. Capping
    first would make relevance unreachable for any query whose match sits late
    in the file -- it is precisely that failure this separation prevents.
    """
    if not tokens:
        return list(rows)
    return [row for row in rows if row_matches_tokens(row, tokens)]


# ---------------------------------------------------------------------------
# Per-resource identity projection
# ---------------------------------------------------------------------------

#: The UI UX Pro Max dataset's verified identity columns.
#:
#: These two -- and ONLY these two -- are promoted into an entry's ``title`` and
#: ``entry_id``. Every other column stays an ordinary bounded ``fields`` entry.
#:
#: The generic reader took ``header[0]`` as the title. On the real VPS dataset
#: that column is ``No`` -- a row ordinal -- so entries shipped as
#: ``title = "4"``. The identity a consumer can cite and a human can read are
#: ``Style Category`` and ``Style ID``, and both are verified-present.
#:
#: Deliberately a **projection of two names, not a parsed schema**. Hardcoding
#: all eighteen verified columns would freeze a snapshot of a third-party CSV
#: into this module and make a schema change look like a code change. Projecting
#: only identity fixes the defect that was actually observed and leaves every
#: other column free to arrive, be added to, or disappear.
UI_UX_PRO_MAX_TITLE_COLUMN = "Style Category"
UI_UX_PRO_MAX_ID_COLUMN = "Style ID"


@dataclass(frozen=True)
class ResourceIdentityProjection:
    """How one resource's rows derive an entry's title and id.

    ``title_column``/``id_column`` are header names to look up. When a column is
    absent, empty, or whitespace on a given row, the caller falls back -- per
    field, per row -- so one row missing ``Style ID`` degrades that row's id
    rather than the whole dataset's.
    """

    title_column: Optional[str] = None
    id_column: Optional[str] = None


#: Projections keyed by resource id. A resource absent from this table keeps the
#: generic behaviour (first column as title, positional id).
RESOURCE_IDENTITY_PROJECTIONS: Dict[str, ResourceIdentityProjection] = {
    "ui_ux_pro_max": ResourceIdentityProjection(
        title_column=UI_UX_PRO_MAX_TITLE_COLUMN,
        id_column=UI_UX_PRO_MAX_ID_COLUMN,
    ),
}


def identity_projection_for(resource_id: str) -> ResourceIdentityProjection:
    """The identity projection for ``resource_id`` (generic when undeclared)."""
    return RESOURCE_IDENTITY_PROJECTIONS.get(
        resource_id, ResourceIdentityProjection()
    )


def _cell_text(row: Mapping[str, str], column: Optional[str]) -> str:
    """A row cell as trimmed text, or ``""`` when absent/blank.

    Missing and blank are treated identically on purpose: a CSV cell holding
    whitespace is not an identity, and falling through on both keeps the
    fallback path to one rule instead of two.
    """
    if not column:
        return ""
    value = row.get(column)
    if not isinstance(value, str):
        return ""
    return value.strip()


def project_entry_identity(
    row: Mapping[str, str],
    header: Sequence[str],
    projection: ResourceIdentityProjection,
    fallback_title: str,
    row_number: int,
) -> Tuple[str, str]:
    """Return ``(title, identity)`` for one row under ``projection``.

    Each field falls back independently and deterministically:

    * **title** -- the projected title column, else the declared fallback column,
      else the row's first non-empty header value, else a positional
      ``"<resource>: row <n>"``.
    * **identity** -- the projected id column, else a positional
      ``"<resource>:<n>"``.

    The positional id is derived from the row's index in the *whole* dataset, so
    it is stable for a given file regardless of how many rows matched a query.
    Two rows can therefore never collide on the fallback id, and a fallback id
    never masquerades as a real ``Style ID``.
    """
    title = _cell_text(row, projection.title_column)
    if not title:
        title = _cell_text(row, header[0] if header else None)
    if not title:
        for column in header:
            candidate = _cell_text(row, column)
            if candidate:
                title = candidate
                break
    if not title:
        title = fallback_title

    identity = _cell_text(row, projection.id_column)
    if not identity:
        identity = f"{fallback_title}:{row_number}"
    return title, identity


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _RawEntry:
    """An adapter's untruncated output, before bounds are applied.

    ``locator`` travels WITH the entry rather than being reconstructed later,
    so provenance always names the file the entry actually came from -- a
    multi-locator adapter cannot misattribute one file's rows to another.

    ``entry_id`` is the adapter's own identity for the row, which may be a real
    dataset key (``Style ID``) or a positional fallback. It is empty when the
    adapter has no better idea, and the caller supplies the generic
    ``<resource>:<index>`` form.
    """

    title: str
    body: str
    fields: Dict[str, str]
    index: int
    locator: str
    entry_id: str = ""


@dataclass(frozen=True)
class AdapterResult:
    """What an adapter produced, or why it produced nothing."""

    entries: Tuple[_RawEntry, ...]
    warnings: Tuple[str, ...] = ()
    ok: bool = True
    #: The row number of the first row this result's ``_RawEntry.index`` values
    #: are relative to. An adapter that filters must report the source position,
    #: not the filtered position, so a fallback identity stays stable for a
    #: given file across different queries.
    index_base: int = 0


@dataclass(frozen=True)
class DesignAdapter:
    """One table row: which resource kind it serves, what it reads, how.

    ``locators`` is the set of skill-root-relative paths this adapter may read.
    Each MUST appear in the resource's manifest ``data_entries`` (or be
    ``SKILL.md``, which D0 already verifies). The cross-check happens in
    :func:`_adapter_locators_allowed`, and an adapter naming anything else is
    rendered inert rather than trusted.
    """

    name: str
    resource_kinds: Tuple[str, ...]
    entry_kind: str
    locators: Tuple[str, ...]
    read: Callable[["DesignAdapterContext"], AdapterResult]
    supports_query: bool = False
    #: True when this adapter cannot read ANY file, because it pins no locators.
    #: Such an adapter is safe to run regardless of capability status: it can
    #: only produce an honest-empty result, never content, so invoking it on a
    #: deferred resource yields the resource-SPECIFIC degradation warning
    #: ("no local, verified content") instead of a generic "unavailable".
    #:
    #: A content adapter MUST set this False, because a file-reading adapter run
    #: against an absent resource would have nothing real to return and could
    #: only ever invent it. The two are therefore checked against each other.
    content_free: bool = False

    def allows_locator(self, locator: str) -> bool:
        return locator in self.locators


#: ``SKILL.md`` is readable by any skill adapter because D0 already proves it
#: exists, is a readable non-empty file, and is contained. It needs no
#: ``data_entries`` pin to be safe.
SKILL_MD_LOCATOR = "SKILL.md"


@dataclass(frozen=True)
class DesignAdapterContext:
    """Everything an adapter is allowed to see.

    Adapters receive this and nothing else: no manifest object, no capability
    report, no network handle, no absolute paths beyond the resolved skill root
    they were given. Narrowing the surface a reader can touch is what keeps the
    trust boundary an implementation fact rather than a convention.
    """

    skill_root: Path
    locator: str
    query: str
    tokens: Tuple[str, ...]
    #: How this resource's rows derive title and id. Always present; the
    #: generic (all-None) projection reproduces the pre-projection behaviour, so
    #: an adapter written against the old context still works.
    projection: ResourceIdentityProjection = ResourceIdentityProjection()
    #: Prefix used to build a positional fallback identity, so a fallback can
    #: never be mistaken for a real dataset key.
    fallback_id_prefix: str = ""


def _csv_text(cell: str) -> str:
    """Coerce one CSV cell to a searchable, bounded string."""
    if not isinstance(cell, str):
        return ""
    return cell


def _guidance_csv(context: DesignAdapterContext) -> AdapterResult:
    """Read a pinned CSV dataset into one entry per data row.

    Generic by design: the first row is the header, every column becomes a
    field, and no column is promoted into authority. The ONLY per-column
    knowledge is the identity projection (:class:`ResourceIdentityProjection`),
    which supplies this resource's ``title``/``entry_id`` and nothing else.

    **No subprocess.** ``scripts/search.py`` is never executed; the dataset is
    read in-process with the stdlib CSV reader. Running a skill's own script is
    a later, explicitly deferred layer.
    """
    path = context.skill_root / context.locator

    try:
            raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
            return AdapterResult(entries=(), warnings=(WARNING_MALFORMED,), ok=False)

    if not raw.strip():
            return AdapterResult(entries=(), warnings=(WARNING_MALFORMED,), ok=False)

    try:
            reader = csv.reader(io.StringIO(raw))
            rows = [row for row in reader]
    except csv.Error:
            return AdapterResult(entries=(), warnings=(WARNING_MALFORMED,), ok=False)

    if not rows:
            return AdapterResult(entries=(), warnings=(WARNING_MALFORMED,), ok=False)

    header = [cell.strip() for cell in rows[0]]
    # A header row of nothing usable is a malformed dataset, not an empty one.
    if not header or not any(header):
            return AdapterResult(entries=(), warnings=(WARNING_MALFORMED,), ok=False)

    # Normalize every row to the header width so a ragged row cannot silently
    # shift values into the wrong column.
    normalized: List[Dict[str, str]] = []
    for row in rows[1:]:
            if not any(cell.strip() for cell in row):
                continue
            padded = list(row) + [""] * (len(header) - len(row))
            normalized.append(
                {header[i]: _csv_text(padded[i]) for i in range(len(header))}
            )

    # Filter over the WHOLE dataset before any cap. See filter_rows.
    #
    # ``enumerate(normalized, 1)`` numbers the SOURCE rows, so a matching row
    # keeps its real position whatever else matched: a late match is both
    # reachable and still identified by where it actually lives.
    numbered = list(enumerate(normalized, 1))
    selected = [
        (number, row)
        for number, row in numbered
        if row_matches_tokens(row, context.tokens)
    ]

    entries: List[_RawEntry] = []
    for index, (number, row) in enumerate(selected):
            title, identity = project_entry_identity(
                row,
                header,
                context.projection,
                context.fallback_id_prefix,
                number,
            )
            entries.append(
                _RawEntry(
                    title=title,
                    body=_row_body(row, header),
                    fields=dict(row),
                    index=number,
                    locator=context.locator,
                    entry_id=identity,
                )
            )

    return AdapterResult(entries=tuple(entries), ok=True)


#: Columns promoted into an entry's ``body``. A CSV row's cells are already
#: fully preserved in ``fields``; body carries a readable rendering so a
#: consumer that only reads text still sees the row.
_BODY_SEPARATOR = " | "


def _row_body(row: Mapping[str, str], header: Sequence[str]) -> str:
    parts = []
    for key in header:
        value = row.get(key, "")
        if value:
            parts.append(f"{key}: {value}")
    return _BODY_SEPARATOR.join(parts)


def _reference_adapter(context: DesignAdapterContext) -> AdapterResult:
    """Reference corpora: honest emptiness.

    Refero, 21st.dev, React Bits and Transitions.dev have **no network
    integration on this host**. The correct outcome is zero entries, a degraded
    flag, and a static warning -- never a synthesized summary of what such a
    corpus "typically contains". Inventing guidance is the precise failure this
    module exists to prevent, and a plausible-looking summary of a corpus nobody
    fetched is the most dangerous form of it.
    """
    return AdapterResult(entries=(), warnings=(WARNING_REFERENCE_UNAVAILABLE,), ok=True)


def _critic_inert_adapter(context: DesignAdapterContext) -> AdapterResult:
    """Impeccable: inert, by design.

    The resource is absent on this host and its real structure has not been
    inspected. D1 therefore ships **no parser** and pins **no** ``data_entries``
    for it. :class:`CriticFinding` declares the canonical schema for whenever a
    real parser is written against a verified resource; nothing here fabricates
    one finding to prove the shape works.
    """
    return AdapterResult(entries=(), warnings=(WARNING_CRITIC_INERT,), ok=True)


def _deferred_adapter(context: DesignAdapterContext) -> AdapterResult:
    """No adapter wired for this resource kind."""
    return AdapterResult(entries=(), warnings=(WARNING_DEFERRED,), ok=True)


#: The adapter table. Small, explicit, table-driven: adding a resource is a row
#: here, not a new bespoke reader.
DESIGN_ADAPTERS: Tuple[DesignAdapter, ...] = (
    DesignAdapter(
        name="guidance",
        resource_kinds=("skill",),
        entry_kind="guidance",
        locators=("data/styles.csv",),
        read=_guidance_csv,
        supports_query=True,
    ),
    DesignAdapter(
        name="critic",
        resource_kinds=("skill",),
        entry_kind="critic_finding",
        locators=(),
        read=_critic_inert_adapter,
            content_free=True,
        ),
        DesignAdapter(
            name="reference",
            resource_kinds=("reference",),
            entry_kind="reference",
            locators=(),
            read=_reference_adapter,
            content_free=True,
        ),
        DesignAdapter(
            name="policy",
            resource_kinds=("registry", "npm_optional"),
            entry_kind="policy",
            locators=(),
            read=_deferred_adapter,
            content_free=True,
        ),
    )


def _adapter_for(resource_id: str, resource: DesignResource) -> Optional[DesignAdapter]:
    """Pick the adapter serving ``resource_id``, or None if none is wired.

    An explicit per-id table takes precedence over kind-based selection. It has
    to: two ``skill`` resources need genuinely different behaviour, and only one
    of them has a verified dataset to read. Selecting by kind alone would hand
    the dataset adapter to a resource whose real structure has never been
    inspected -- which is exactly the invented-contract mistake this batch
    exists not to make.

    The id table is therefore a *restriction*, never an expansion: an id not
    named there falls back to kind selection, and every adapter's locators are
    still cross-checked against the manifest before a single byte is read.
    """
    pinned = _RESOURCE_ADAPTER_IDS.get(resource_id)
    if pinned is not None:
        return pinned
    for adapter in DESIGN_ADAPTERS:
        if resource.kind in adapter.resource_kinds:
            return adapter
    return None


def _adapter_by_name(name: str) -> DesignAdapter:
    for adapter in DESIGN_ADAPTERS:
        if adapter.name == name:
            return adapter
    raise KeyError(name)


#: Per-resource adapter pinning.
#:
#: ``impeccable`` is pinned to the CRITIC adapter, which is inert: it pins no
#: locators and ships no parser. Pinning it explicitly (rather than letting it
#: inherit the guidance adapter by ``kind``) is what guarantees the dataset
#: reader can never be handed a resource whose real structure is uninspected,
#: and it lets the critic's honest, resource-specific degradation warning
#: surface instead of a generic "unavailable".
_RESOURCE_ADAPTER_IDS: Dict[str, DesignAdapter] = {
    "impeccable": _adapter_by_name("critic"),
    "ui_ux_pro_max": _adapter_by_name("guidance"),
}


# ---------------------------------------------------------------------------
# Manifest / adapter cross-check
# ---------------------------------------------------------------------------


def _adapter_locators_allowed(
    adapter: DesignAdapter, resource: DesignResource
) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    """Split the adapter's locators into permitted and undeclared.

    An adapter may read only a locator the manifest already declares in
    ``data_entries``, or ``SKILL.md``.

    The second return value is the *rejection list*: locators the adapter asked
    for that the manifest does not sanction. Naming one renders the adapter
    **inert** -- zero entries plus a static warning -- rather than an import
    error, because an adapter naming an unpinned file is a policy mistake, not
    a crash. Nothing outside the permitted set is ever read.

    This is the check that keeps the adapter table honest against the manifest.
    A YAML copy of this mapping would drift; the cross-check cannot.
    """
    if not adapter.locators:
        return (), ()

    permitted: List[str] = []
    undeclared: List[str] = []
    declared = set(resource.data_entries)

    for locator in adapter.locators:
        if locator == SKILL_MD_LOCATOR or locator in declared:
            permitted.append(locator)
        else:
            undeclared.append(locator)

    return tuple(permitted), tuple(undeclared)


# ---------------------------------------------------------------------------
# Retrieval
# ---------------------------------------------------------------------------


def _resolve_skill_root(
    hermes_home: Path, resource: DesignResource
) -> Tuple[Optional[Path], Optional[str]]:
    """Resolve and re-verify a skill resource's root.

    The containment re-check is the *resolved* half of D0's two-part guard, and
    it runs on **every** read rather than trusting the preflight. Preflight ran
    in a different process, possibly against a different filesystem state;
    re-verifying at the point of read is what makes containment a property of
    the read itself.
    """
    if resource.skill_name is None:
        return None, "skill_name is not declared"
    skill_dir = design_profile_skills_dir(hermes_home) / resource.skill_name
    if not skill_dir.is_dir():
        return None, "skill directory not present in profile"
    if not entry_is_contained(skill_dir, skill_dir):
        return None, "skill directory resolves outside the profile"
    return skill_dir, None


def retrieve_design_guidance(
    hermes_home: Path,
    requested: Sequence[str],
    *,
    query: str = "",
    limits: Optional[DesignContextLimits] = None,
    manifest: Optional[DesignResourceManifest] = None,
    capability_report: Optional[DesignCapabilityReport] = None,
) -> DesignRetrievalReport:
    """Retrieve bounded, provenance-carrying design entries for ``requested``.

    ``hermes_home`` is supplied by the caller; this module never reads
    ``HERMES_HOME`` itself, mirroring :func:`resolve_design_capabilities`.

    D1 reads capability STATUS and never re-derives it. If no
    ``capability_report`` is passed, one is resolved here so the authority stays
    single-sourced.

    ``requested`` is processed in the order given. Order is significant: it is
    the first-N-wins order for ``max_resources``, and preserving it is what
    makes truncation deterministic.

    **No availability exception is raised.** The report carries ``ok``, mirroring
    :func:`resolve_design_capabilities`: an absent optional resource is a
    degraded outcome that a caller logs, not a startup blocker.

    Raises only :class:`DesignResourceManifestError` -- D0's own contract, for
    an undeclared resource id or an unreadable manifest. A typo in a resource
    name is a bug that should be loud.
    """
    if limits is None:
        limits = DEFAULT_LIMITS

    if manifest is None:
        manifest = load_design_resource_manifest()

    if capability_report is None:
        capability_report = resolve_design_capabilities(hermes_home, manifest)

    # Bound the request set first, in the caller's order.
    ordered = list(requested)[: max(0, limits.max_resources)]
    resource_limit_hit = len(ordered) < len(requested)

    results: Dict[str, DesignResourceResult] = {}
    resolved: List[str] = []
    unavailable_optional: List[str] = []
    ok = True
    truncated = resource_limit_hit
    total_chars = 0
    total_entries = 0

    if resource_limit_hit:
        logger.info(
            "Design retrieval honored max_resources=%d of %d requested ids.",
            limits.max_resources,
            len(requested),
        )

    for resource_id in ordered:
        resource = manifest.get(resource_id)
        capability = capability_report.resources.get(resource_id)

        result, consumed = _retrieve_one(
            hermes_home=hermes_home,
            resource=resource,
            capability=capability,
            query=query,
            limits=limits,
        )
        results[resource_id] = result

        if result.status == STATUS_UNAVAILABLE_REQUIRED:
            ok = False
        if result.status == STATUS_UNAVAILABLE_OPTIONAL:
            unavailable_optional.append(resource_id)
        if result.status in (STATUS_AVAILABLE, STATUS_NOT_INSTALLED):
            resolved.append(resource_id)

        # Aggregate budget: stop at the RESOURCE boundary. Part of a resource is
            # never partially applied -- a half-read resource is a partial-as-success.
            if total_chars + consumed > limits.max_total_chars:
                results[resource_id] = _dropped_for_budget(
                    resource,
                    result.status,
                    result.available,
                )
                truncated = True
                logger.info(
                    "Design retrieval stopped at the total character budget; "
                    "%s was not retrieved.",
                    resource_id,
                )
                continue

        total_chars += consumed
        total_entries += len(result.entries)
        if result.truncated:
            truncated = True

    if truncated:
        logger.info("Design retrieval summary: %s", _preview_report(results))

    report = DesignRetrievalReport(
        ok=ok,
        requested=tuple(ordered),
        resolved=tuple(resolved),
        unavailable_optional=tuple(unavailable_optional),
        results=results,
        total_entries=total_entries,
        total_chars=total_chars,
        truncated=truncated,
        limits=limits.to_dict(),
    )

    logger.info("Design retrieval: %s", report.summary())
    return report


def _preview_report(results: Mapping[str, DesignResourceResult]) -> str:
    """Bounded, payload-free preview used for the truncation log line."""
    return ", ".join(
        f"{rid}={len(result.entries)}entries"
        f"{'/' + str(result.dropped_entries) + 'dropped' if result.dropped_entries else ''}"
        for rid, result in sorted(results.items())
    )


def _empty_result(
    resource: DesignResource,
    status: str,
    available: bool,
    *,
    warning: Optional[str] = None,
) -> DesignResourceResult:
    """A zero-entry result carrying only status and a static warning."""
    warnings = (warning,) if warning else ()
    return DesignResourceResult(
        resource_id=resource.resource_id,
        resource_kind=resource.kind,
        status=status,
        available=available,
        degraded=bool(warning),
        entries=(),
        warnings=warnings,
        truncated=False,
        dropped_entries=0,
    )


def _dropped_for_budget(
    resource: DesignResource,
    status: str,
    available: bool,
) -> DesignResourceResult:
    """A resource skipped by the aggregate budget.

    ``truncated`` is True: content WAS produced and then deliberately not
    shipped, which is exactly what a consumer needs to know to raise its
    budget. An empty result that merely matched nothing is not a truncation,
    and conflating the two would make "raise the limit" the response to a
    search that simply found nothing.
    """
    return DesignResourceResult(
        resource_id=resource.resource_id,
        resource_kind=resource.kind,
        status=status,
        available=available,
        degraded=True,
        entries=(),
        warnings=(WARNING_DROPPED_FOR_BUDGET,),
        truncated=True,
        dropped_entries=0,
    )


def _retrieve_one(
    *,
    hermes_home: Path,
    resource: DesignResource,
    capability: Optional[Any],
    query: str,
    limits: DesignContextLimits,
) -> Tuple[DesignResourceResult, int]:
    """Retrieve one resource and report its canonical char consumption.

    The returned int is the resource's payload size *after* all bounds, which
    is what the aggregate budget must count -- measured by the same function
    the per-entry cap uses.
    """
    status = capability.status if capability is not None else STATUS_UNAVAILABLE_OPTIONAL
    available = bool(capability.available) if capability is not None else False

    # --- Policy-only resources: never retrieved as content -----------------
    #
    # ``degraded`` is False for both branches. These resources have no content
    # to retrieve BY CONSTRUCTION, so asking for their content is a category
    # error, not a degradation -- and ``not_installed`` is the correct resting
    # state that nobody can act on at bootstrap. Reporting either as a
    # degradation teaches operators to ignore real degradations, which is the
    # exact failure ``not_installed`` was introduced to avoid.
    if resource.kind in POLICY_ONLY_RESOURCE_KINDS:
        return (
            DesignResourceResult(
                resource_id=resource.resource_id,
                resource_kind=resource.kind,
                status=status,
                available=False,
                degraded=False,
                entries=(),
                warnings=(WARNING_POLICY_ONLY,),
                truncated=False,
                dropped_entries=0,
            ),
            0,
        )

    adapter = _adapter_for(resource.resource_id, resource)

    # --- Not available ---------------------------------------------------
    #
    # A content-FREE adapter (one that pins no locators and therefore cannot
    # read a file even if asked) is still run, because it is the thing that
    # knows WHY this resource yields nothing -- "this corpus has no local
    # verified content" is a materially different statement from "unavailable",
    # and only the adapter can tell them apart.
    #
    # A CONTENT adapter is never run against an absent resource. It has no real
    # file to read, so anything it returned would be invented, and invention is
    # the one outcome this batch must never produce.
    if status != STATUS_AVAILABLE:
        if status == STATUS_NOT_INSTALLED:
            # Resting state, not a degradation.
            return (
                DesignResourceResult(
                    resource_id=resource.resource_id,
                    resource_kind=resource.kind,
                    status=status,
                    available=False,
                    degraded=False,
                    entries=(),
                    warnings=(),
                    truncated=False,
                    dropped_entries=0,
                ),
                0,
            )

        if adapter is not None and adapter.content_free:
            empty_result = adapter.read(
                DesignAdapterContext(
                    skill_root=Path(),
                    locator="",
                    query=query,
                    tokens=normalize_query_tokens(query),
                )
            )
            return (
                DesignResourceResult(
                    resource_id=resource.resource_id,
                    resource_kind=resource.kind,
                    status=status,
                    available=False,
                    degraded=True,
                    entries=(),
                    warnings=tuple(empty_result.warnings),
                    truncated=False,
                    dropped_entries=0,
                ),
                0,
            )

        return (
            _empty_result(resource, status, available, warning=WARNING_ABSENT),
            0,
        )

    if adapter is None:
        return (
            _empty_result(
                resource,
                STATUS_AVAILABLE,
                True,
                warning=WARNING_DEFERRED,
            ),
            0,
        )

    # --- Available: resolve root, re-check containment ---------------------
    skill_root, root_problem = _resolve_skill_root(hermes_home, resource)
    if skill_root is None or root_problem is not None:
        return (
            _empty_result(
                resource,
                STATUS_UNAVAILABLE_OPTIONAL if not resource.required else STATUS_UNAVAILABLE_REQUIRED,
                False,
                warning=WARNING_ABSENT,
            ),
            0,
        )

    adapter = _adapter_for(resource.resource_id, resource)
    if adapter is None:
        return (
            _empty_result(
                resource,
                STATUS_AVAILABLE,
                True,
                warning=WARNING_DEFERRED,
            ),
            0,
        )

    permitted, undeclared = _adapter_locators_allowed(adapter, resource)
    if undeclared:
        # An adapter naming an unpinned locator is rendered INERT: zero entries,
        # a static warning, and no file is opened. Never an import error.
        return (
            _empty_result(
                resource,
                STATUS_AVAILABLE,
                True,
                warning=WARNING_UNDECLARED_LOCATOR,
            ),
            0,
        )

    warnings: List[str] = []
    tokens = normalize_query_tokens(query)

    raw_entries: List[_RawEntry] = []
    adapter_failed = False
    for locator in permitted:
        path = skill_root / locator
        # Containment re-check on EVERY read, not just at preflight.
        if not entry_is_contained(skill_root, path):
            adapter_failed = True
            warnings.append(WARNING_UNDECLARED_LOCATOR)
            continue
        if not path.is_file():
            continue
        context = DesignAdapterContext(
            skill_root=skill_root,
            locator=locator,
            query=query,
            tokens=tokens,
                        projection=identity_projection_for(resource.resource_id),
                        fallback_id_prefix=resource.resource_id,
                    )
        adapter_result = adapter.read(context)
        raw_entries.extend(adapter_result.entries)
        warnings.extend(adapter_result.warnings)
        if not adapter_result.ok:
            adapter_failed = True

    if adapter_failed and not raw_entries:
        # Malformed output fails closed. Partial-as-success is worse than none.
        return (
            _empty_result(resource, STATUS_AVAILABLE, True, warning=WARNING_MALFORMED),
            0,
        )

    # --- Normalize, then bound --------------------------------------------
    entries: List[DesignEntry] = []
    dropped = 0
    for raw in raw_entries:
        if len(entries) >= limits.max_entries_per_resource:
            dropped += 1
            continue
        provenance = EntryProvenance(
            resource_id=resource.resource_id,
            resource_kind=resource.kind,
            adapter=adapter.name,
                        locator=raw.locator,
            entry_index=raw.index,
        )
        entry = DesignEntry(
                        entry_id=raw.entry_id or f"{resource.resource_id}:{raw.index}",
            kind=adapter.entry_kind,
            title=raw.title,
            body=raw.body,
            fields=raw.fields,
            provenance=provenance,
            truncated=False,
        )
        shrunk = _shrink_entry_to_budget(entry, limits.max_entry_chars)
        if shrunk is None:
            # Cannot fit even mandatory metadata: drop rather than over-emit.
            dropped += 1
            continue
        entries.append(shrunk)

    resource_truncated = dropped > 0
    if dropped:
        warnings.append(WARNING_TRUNCATED_ENTRIES)

    # Per-resource cap, measured with the SAME size function as the entry cap.
    kept: List[DesignEntry] = []
    running = 0
    for entry in entries:
        size = payload_chars(entry)
        if running + size > limits.max_resource_chars:
            dropped += 1
            resource_truncated = True
            continue
        kept.append(entry)
        running += size

    if resource_truncated:
        warnings.append(WARNING_TRUNCATED_RESOURCE)

    result = DesignResourceResult(
        resource_id=resource.resource_id,
        resource_kind=resource.kind,
        status=STATUS_AVAILABLE,
        available=True,
        degraded=bool(warnings) and not entries,
        entries=tuple(kept),
        warnings=tuple(dict.fromkeys(warnings)),
        truncated=resource_truncated,
        dropped_entries=dropped,
    )
    return result, running


__all__ = [
    "DEFAULT_LIMITS",
    "ENTRY_KINDS",
    "MAX_QUERY_CHARS",
    "MAX_QUERY_TOKEN_CHARS",
    "SKILL_MD_LOCATOR",
    "TRUNCATION_MARKER",
    "AdapterResult",
    "CriticFinding",
    "DesignAdapter",
    "DesignAdapterContext",
    "DesignContextLimits",
    "DesignEntry",
    "DesignResourceResult",
    "DesignRetrievalReport",
    "EntryProvenance",
        "UI_UX_PRO_MAX_ID_COLUMN",
        "UI_UX_PRO_MAX_TITLE_COLUMN",
        "RESOURCE_IDENTITY_PROJECTIONS",
        "ResourceIdentityProjection",
        "filter_rows",
        "identity_projection_for",
        "lexical_tokens",
        "normalize_query_tokens",
        "payload_chars",
        "retrieve_design_guidance",
        "row_matches_tokens",
    ]
