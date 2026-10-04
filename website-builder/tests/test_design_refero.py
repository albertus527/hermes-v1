"""Batch D3a.5 Part E: Refero local-skill activation and bounded retrieval.

Real temporary profiles with real Markdown reference trees laid out like the
upstream ``referodesign/refero_skill`` ``skills/refero-design/`` directory. No
network, no subprocess, no install.

The properties under test are BEHAVIOUR CONTRACTS:

    * the whole canonical skill directory is required, not SKILL.md alone
    * the free local baseline works with NO credential
    * the paid live-MCP tier is an optional enhancement that never gates it, and
      its absence is not a degradation
    * retrieval is bounded: entries, characters, deterministic ordering
    * provenance is skill-root-relative and containment is re-verified at read
    * a declared-but-absent locator is never read
    * nothing is invented: entries carry the file's own prose
"""

from __future__ import annotations

import dataclasses
import json
import socket
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_activation import (
    REASON_AUTH_OPTIONAL,
    REASON_LOCALLY_PROVISIONED,
    REASON_SKILL_ARTIFACTS_MISSING,
    REASON_SKILL_NOT_PROVISIONED,
    activate_design_resources,
)
from app.core.design_capabilities import (
    STATUS_AVAILABLE,
    STATUS_UNAVAILABLE_OPTIONAL,
    resolve_design_capabilities,
)
from app.core.design_resources import load_design_resource_manifest
from app.core.design_retrieval import (
    DesignContextLimits,
    payload_chars,
    retrieve_design_guidance,
)

SHIPPED_MANIFEST = (
    Path(__file__).resolve().parent.parent / "config" / "design_resources.yaml"
)

REFERO = "refero"

#: The reference files the manifest pins, mirroring the upstream layout.
REFERENCES = (
    "references/typography.md",
    "references/color.md",
    "references/motion.md",
    "references/craft-details.md",
    "references/anti-ai-slop.md",
)

TYPOGRAPHY = """# Typography

An opening paragraph before any subheading.

## Line length

Aim for 45 to 75 characters per line for comfortable reading.

## Modular scale

Use a scale ratio near 1.25 for a calm hierarchy.
"""

COLOR = """# Color

## Contrast

Never drop below 4.5:1 for body text on any surface.

## Palette temperature

Pick one temperature and hold it across the whole system.
"""


@pytest.fixture(autouse=True)
def _no_side_effects(monkeypatch):
    """A local skill is read in-process; it is never fetched or executed."""

    def deny(*args, **kwargs):
        raise AssertionError("local reference retrieval must not do this")

    monkeypatch.setattr(subprocess, "Popen", deny)
    monkeypatch.setattr(subprocess, "run", deny)
    monkeypatch.setattr(socket.socket, "connect", deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


@pytest.fixture
def manifest():
    return load_design_resource_manifest(SHIPPED_MANIFEST)


@pytest.fixture
def home(tmp_path) -> Path:
    root = tmp_path / "profile"
    (root / "skills").mkdir(parents=True)
    return root


def _provision(home: Path, *, complete: bool = True, files=REFERENCES) -> Path:
    """Provision the skill the way setup/deploy would: the WHOLE directory."""
    skill = home / "skills" / "refero-design"
    (skill / "agents").mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: refero-design\n---\n\nCraft guidance.\n", encoding="utf-8"
    )
    (skill / "agents" / "openai.yaml").write_text("name: refero\n", encoding="utf-8")

    bodies = {
        "references/typography.md": TYPOGRAPHY,
        "references/color.md": COLOR,
    }
    for relative in files:
        target = skill / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            bodies.get(relative, f"# {Path(relative).stem}\n\n## Rule\n\nGuidance.\n"),
            encoding="utf-8",
        )

    if not complete:
        # Remove one pinned artifact: the whole directory IS required.
        (skill / files[-1]).unlink()
    return skill


def _activate(home: Path, manifest):
    return activate_design_resources(home, manifest, system="linux", machine="x86_64")


def _limits(**overrides) -> DesignContextLimits:
    """Generous defaults with specific bounds tightened per test.

    Built with ``dataclasses.replace`` because the limits object is frozen --
    mutating it would defeat the point of an immutable bound.
    """
    base = DesignContextLimits(
        max_resources=1,
        max_entries_per_resource=20,
        max_entry_chars=4000,
        max_resource_chars=40000,
        max_total_chars=60000,
    )
    return dataclasses.replace(base, **overrides)


# ---------------------------------------------------------------------------
# The whole directory is required
# ---------------------------------------------------------------------------


def test_the_manifest_pins_the_reference_files_not_just_skill_md(manifest):
    """SKILL.md alone is the placeholder directory this batch removes."""
    entries = set(manifest.get(REFERO).data_entries)

    assert "SKILL.md" in entries
    reference_entries = {e for e in entries if e.startswith("references/")}
    assert len(reference_entries) >= 3, (
        "the reference files ARE the craft guidance; pinning only SKILL.md "
        "would certify a placeholder"
    )


def test_the_manifest_names_the_canonical_skill_directory(manifest):
    resource = manifest.get(REFERO)

    assert resource.kind == "skill"
    assert resource.resolution == "profile_skill"
    assert resource.skill_name == "refero-design"
    assert resource.required is False


def test_a_complete_skill_is_available(home, manifest):
    _provision(home)

    report = resolve_design_capabilities(home, manifest)

    assert report.resources[REFERO].status == STATUS_AVAILABLE


def test_a_skill_missing_one_reference_is_not_a_capability(home, manifest):
    """Partial provisioning is the case this distinction exists to catch."""
    _provision(home, complete=False)

    report = resolve_design_capabilities(home, manifest)
    capability = report.resources[REFERO]

    assert capability.status == STATUS_UNAVAILABLE_OPTIONAL
    assert capability.available is False
    assert REASON_SKILL_ARTIFACTS_MISSING in _activate(home, manifest).capabilities[
        REFERO
    ].reasons


def test_an_unprovisioned_skill_reports_honest_absence(home, manifest):
    capability = _activate(home, manifest).capabilities[REFERO]

    assert capability.usable is False
    assert REASON_SKILL_NOT_PROVISIONED in capability.reasons


# ---------------------------------------------------------------------------
# The free baseline never depends on the paid tier
# ---------------------------------------------------------------------------


def test_the_local_baseline_works_with_no_credential(home, manifest, monkeypatch):
    """Refero's bundled references are FREE; no account is involved."""
    monkeypatch.delenv("REFERO_API_KEY", raising=False)
    _provision(home)

    capability = _activate(home, manifest).capabilities[REFERO]

    assert capability.discovery_available is True
    assert capability.retrieval_available is True
    assert capability.locally_provisioned is True
    assert capability.degraded is False, (
        "the free baseline is working as designed; the absent paid tier is not "
        "a degradation"
    )


def test_the_paid_tier_is_reported_separately(home, manifest, monkeypatch):
    """Present or not, the paid tier is reported on its own axis."""
    monkeypatch.delenv("REFERO_API_KEY", raising=False)
    _provision(home)

    without = _activate(home, manifest).capabilities[REFERO]

    # The capability being reported is the FREE local craft references. It is
    # not gated, so `authentication_required` must be False -- claiming otherwise
    # reports a blocker that does not exist. The unmet paid tier is reported as
    # OPTIONAL, which is the accurate statement.
    assert without.authentication_required is False
    assert without.authentication_optional is True
    assert without.authentication_present is False
    assert REASON_AUTH_OPTIONAL in without.reasons
    assert without.usable is True, (
        "the free baseline is fully usable with no account at all"
    )

    monkeypatch.setenv("REFERO_API_KEY", "configured")
    with_key = _activate(home, manifest).capabilities[REFERO]

    assert with_key.authentication_present is True
    assert with_key.retrieval_available is True, (
        "having the credential must not be the thing that breaks the baseline"
    )
    assert with_key.locally_provisioned is True


def test_an_absent_paid_tier_never_disables_the_local_skill(home, manifest, monkeypatch):
    monkeypatch.delenv("REFERO_API_KEY", raising=False)
    _provision(home)

    capability = _activate(home, manifest).capabilities[REFERO]

    assert capability.usable is True


def test_the_credential_value_never_reaches_the_report(home, manifest, monkeypatch):
    monkeypatch.setenv("REFERO_API_KEY", "sk-refero-secret")
    _provision(home)

    rendered = json.dumps(_activate(home, manifest).to_dict())

    assert "sk-refero-secret" not in rendered


# ---------------------------------------------------------------------------
# Bounded retrieval of real content
# ---------------------------------------------------------------------------


def test_retrieval_returns_real_file_prose(home, manifest):
    _provision(home)

    report = retrieve_design_guidance(home, [REFERO], manifest=manifest)
    result = report.results[REFERO]

    assert report.ok is True
    assert result.entries, "a provisioned skill must yield real entries"
    titles = {entry.title for entry in result.entries}
    assert "Line length" in titles
    assert "Modular scale" in titles
    bodies = " ".join(entry.body for entry in result.entries)
    assert "45 to 75 characters" in bodies, "the file's own prose must survive"


def test_retrieval_splits_on_headings_so_sections_are_citable(home, manifest):
    _provision(home)

    result = retrieve_design_guidance(home, [REFERO], manifest=manifest).results[REFERO]

    assert any(entry.title == "Typography" for entry in result.entries)
    assert any(entry.title == "Color" for entry in result.entries)


def test_content_before_the_first_heading_is_not_dropped(home, manifest):
    """A reference's opening paragraph is guidance too."""
    _provision(home)

    result = retrieve_design_guidance(home, [REFERO], manifest=manifest).results[REFERO]

    assert any(
        "opening paragraph" in entry.body for entry in result.entries
    ), "preamble content must be retrieved, not silently discarded"


def test_provenance_is_skill_relative_and_carries_no_absolute_path(home, manifest):
    _provision(home)

    result = retrieve_design_guidance(home, [REFERO], manifest=manifest).results[REFERO]

    assert result.entries
    for entry in result.entries:
        locator = entry.provenance.locator
        assert locator.startswith("references/")
        assert str(home) not in json.dumps(entry.to_dict())
        assert entry.provenance.resource_id == REFERO


def test_a_query_filters_sections_without_losing_the_rest(home, manifest):
    """Filtering happens before the entry cap, so a late match is reachable."""
    _provision(home)

    report = retrieve_design_guidance(
        home, [REFERO], query="line length", manifest=manifest
    )
    result = report.results[REFERO]

    assert result.entries
    assert any("Line length" == entry.title for entry in result.entries)


def test_the_entry_count_limit_is_enforced(home, manifest):
    _provision(home)

    report = retrieve_design_guidance(
        home, [REFERO], limits=_limits(max_entries_per_resource=2), manifest=manifest
    )
    result = report.results[REFERO]

    assert len(result.entries) <= 2
    assert result.dropped_entries >= 0


def test_the_per_resource_character_limit_is_enforced(home, manifest):
    _provision(home)

    result = retrieve_design_guidance(
        home, [REFERO], limits=_limits(max_resource_chars=200), manifest=manifest
    ).results[REFERO]

    total = sum(payload_chars(entry) for entry in result.entries)
    assert total <= 200, "the resource bound must hold over the whole payload"


def test_the_aggregate_limit_is_enforced(home, manifest):
    _provision(home)

    report = retrieve_design_guidance(
        home, [REFERO], limits=_limits(max_total_chars=150), manifest=manifest
    )

    assert report.total_chars <= 150


def test_ordering_is_deterministic(home, manifest):
    _provision(home)

    first = retrieve_design_guidance(home, [REFERO], manifest=manifest)
    second = retrieve_design_guidance(home, [REFERO], manifest=manifest)

    assert [e.title for e in first.results[REFERO].entries] == [
        e.title for e in second.results[REFERO].entries
    ]


def test_retrieval_reports_honestly_when_the_skill_is_absent(home, manifest):
    report = retrieve_design_guidance(home, [REFERO], manifest=manifest)
    result = report.results[REFERO]

    assert result.entries == ()
    assert result.status != STATUS_AVAILABLE


# ---------------------------------------------------------------------------
# Containment and the locator cross-check
# ---------------------------------------------------------------------------


def test_an_absent_skill_never_yields_entries(home, manifest):
    """Nothing is invented for a resource that was never provisioned."""
    result = retrieve_design_guidance(home, [REFERO], manifest=manifest).results[REFERO]

    assert result.entries == (), "an absent skill must produce zero entries"


def test_an_empty_reference_file_yields_no_invented_content(home, manifest):
    skill = _provision(home)
    (skill / "references" / "color.md").write_text("", encoding="utf-8")

    result = retrieve_design_guidance(home, [REFERO], manifest=manifest).results[REFERO]

    bodies = " ".join(entry.body for entry in result.entries)
    assert "4.5:1" not in bodies, "content must not survive from a emptied file"


def test_a_traversing_manifest_entry_is_rejected_at_load(tmp_path):
    """The syntactic half of containment runs before any filesystem work."""
    import yaml

    from app.core.design_resources import DesignResourceManifestError

    document = yaml.safe_load(SHIPPED_MANIFEST.read_text(encoding="utf-8"))
    document["resources"][REFERO]["data_entries"] = ["../../etc/passwd"]
    path = tmp_path / "manifest.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")

    with pytest.raises(DesignResourceManifestError):
        load_design_resource_manifest(path)