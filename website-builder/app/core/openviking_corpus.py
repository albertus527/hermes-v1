"""D4a.1: the reviewed design-corpus definition (application-owned).

The initial live corpus is a small, REVIEWED set of Hermes Website design
references that already exist on the host -- the provisioned ``refero-design``
and ``impeccable`` skills. This module is the single declaration of WHICH files
are eligible, what CATEGORY each belongs to, and its TRUST classification. It
is policy, not mechanism: ingestion still runs through D4a's
:func:`~app.core.openviking_library.ingest_sources`, so the allowlist, digest,
revision pinning, size bounds, forbidden-source rejection, and idempotency are
all the D4a ones.

WHAT IS DELIBERATELY ABSENT
---------------------------
* No whole-repository import. Only the enumerated reference files.
* No third-party crawl. There is no URL fetcher here.
* No runtime logs, ``.env``, secrets, dependency directories, or generated
  application files -- and :func:`~app.core.openviking_library.is_forbidden_source`
  refuses them again at ingest time.
* No file that does not exist: :func:`build_corpus_specs` reports a missing
  source as ``missing`` rather than inventing a spec for it.

TRUST
-----
Every file below is application-provisioned and reviewed, so its trust is
``reviewed`` (the highest level). Content that merely LOOKS authoritative is
never promoted; trust travels with the record and is surfaced by the adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.core.openviking_library import CATEGORIES, SourceSpec

#: A reviewed reference file and how it is classified.
@dataclass(frozen=True)
class CorpusEntry:
    """One reviewed source: profile-relative path, category, and trust."""

    source_id: str
    rel_path: str          # path relative to the profile skills dir
    category: str
    trust: str = "reviewed"
    content_type: str = "text/markdown"


#: The reviewed corpus. Bounded (well under MAX_INGEST_RESOURCES) and drawn
#: only from provisioned, reviewed skills. Categories map each file to the
#: D4a closed category vocabulary.
CORPUS: Tuple[CorpusEntry, ...] = (
    CorpusEntry("refero_typography", "refero-design/references/typography.md", "design_dna"),
    CorpusEntry("refero_color", "refero-design/references/color.md", "design_dna"),
    CorpusEntry("refero_anti_ai_slop", "refero-design/references/anti-ai-slop.md", "design_dna"),
    CorpusEntry("refero_visual_workflow", "refero-design/references/visual-workflow.md", "design_dna"),
    CorpusEntry("refero_motion", "refero-design/references/motion.md", "motion"),
    CorpusEntry("refero_craft_details", "refero-design/references/craft-details.md", "components"),
    CorpusEntry("refero_icons", "refero-design/references/icons.md", "components"),
    CorpusEntry("impeccable_skill", "impeccable/SKILL.md", "design_dna"),
    CorpusEntry("impeccable_critique", "impeccable/reference/critique.md", "design_dna"),
    CorpusEntry("impeccable_layout", "impeccable/reference/layout.md", "components"),
    CorpusEntry("impeccable_audit", "impeccable/reference/audit.md", "components"),
)


@dataclass(frozen=True)
class CorpusFile:
    """A resolved corpus entry: whether it exists, and its absolute path."""

    entry: CorpusEntry
    absolute_path: Optional[Path]
    exists: bool

    @property
    def locator(self) -> str:
        return self.entry.rel_path


def resolve_corpus(profile_skills_dir: Path) -> List[CorpusFile]:
    """Resolve every corpus entry against ``profile_skills_dir``.

    A missing file is reported as ``exists=False`` and is NOT turned into a
    ``SourceSpec`` by :func:`build_corpus_specs`, so ingestion can never claim a
    source it cannot read.
    """
    base = Path(profile_skills_dir)
    resolved: List[CorpusFile] = []
    for entry in CORPUS:
        path = (base / entry.rel_path).resolve()
        try:
            inside = base.resolve() in path.parents or path.parent == base.resolve()
        except Exception:
            inside = False
        exists = inside and path.is_file()
        resolved.append(
            CorpusFile(entry=entry, absolute_path=path if exists else None, exists=exists)
        )
    return resolved


def build_corpus_specs(profile_skills_dir: Path, project_id: str) -> List[SourceSpec]:
    """The allowlisted :class:`SourceSpec` list for the reviewed corpus.

    Only files that actually exist become specs. The locator is the
    profile-relative path, so the reader root is ``profile_skills_dir``.
    """
    specs: List[SourceSpec] = []
    for corpus_file in resolve_corpus(profile_skills_dir):
        if not corpus_file.exists:
            continue
        entry = corpus_file.entry
        specs.append(
            SourceSpec(
                source_id=entry.source_id,
                project_id=project_id,
                category=entry.category,
                trust=entry.trust,
                locator=entry.rel_path,
                content_type=entry.content_type,
            )
        )
    return specs


def missing_corpus_entries(profile_skills_dir: Path) -> List[str]:
    """The rel paths of declared corpus entries that are absent on this host."""
    return [f.entry.rel_path for f in resolve_corpus(profile_skills_dir) if not f.exists]


def corpus_summary(profile_skills_dir: Path, project_id: str) -> Dict[str, Any]:
    """A bounded, secret-free summary of the corpus for a manifest."""
    resolved = resolve_corpus(profile_skills_dir)
    present = [f for f in resolved if f.exists]
    return {
        "project_id": project_id,
        "declared": len(resolved),
        "present": len(present),
        "missing": [f.entry.rel_path for f in resolved if not f.exists],
        "categories": sorted({f.entry.category for f in present}),
        "trust_levels": sorted({f.entry.trust for f in present}),
        "entries": [
            {
                "source_id": f.entry.source_id,
                "rel_path": f.entry.rel_path,
                "category": f.entry.category,
                "trust": f.entry.trust,
                "present": f.exists,
            }
            for f in resolved
        ],
    }


def _validate_corpus() -> None:
    """Fail-closed self-check: every declared category is a real category."""
    for entry in CORPUS:
        if entry.category not in CATEGORIES:
            raise ValueError(f"corpus entry {entry.source_id!r} has an unknown category")


_validate_corpus()


__all__ = [
    "CORPUS",
    "CorpusEntry",
    "CorpusFile",
    "build_corpus_specs",
    "corpus_summary",
    "missing_corpus_entries",
    "resolve_corpus",
]
