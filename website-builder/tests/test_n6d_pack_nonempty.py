"""N6-D regression — non-empty DesignContextPack serialization + capability wiring.

Pins the two defects fixed for the Forma Atelier E2E:

1. **Capability wiring.** ``build_design_context_pack`` must prove reachability
   from the AUTHORITATIVE ``DesignCapabilityReport``. Without it, a reference is
   assumed unreachable and the pack degrades to ``resources=0`` even though the
   corpus is present and retrieval would succeed.
2. **Non-empty serialization.** The pack stores each resource as
   ``DesignResourceResult.to_dict()`` (entries are DICTS). ``payload_chars``
   must measure that shape -- not only the ``DesignEntry`` dataclass -- or
   ``DesignContextPack.total_chars`` raises ``AttributeError`` the moment a pack
   carries a real entry. Malformed shapes must raise the NAMED
   ``DesignEntryError`` instead of an opaque attribute error.

These use REAL resource payload shapes (a stub adapter producing real
``DesignResourceResult`` objects), never empty packs.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.design_capabilities import (  # noqa: E402
    DesignCapability,
    DesignCapabilityReport,
    STATUS_AVAILABLE,
)
from app.core.design_context import (  # noqa: E402
    DesignContextPack,
    _iter_entries,
    build_design_context_pack,
)
from app.core.design_retrieval import (  # noqa: E402
    DesignEntry,
    DesignEntryError,
    DesignResourceResult,
    EntryProvenance,
    payload_chars,
)
from app.core.design_resources import load_design_resource_manifest  # noqa: E402

GUIDANCE = "ui_ux_pro_max"


# ---------------------------------------------------------------------------
# Real payload fixtures (the shapes the pack actually carries)
# ---------------------------------------------------------------------------

def _provenance(resource_id: str) -> EntryProvenance:
    return EntryProvenance(
        resource_id=resource_id,
        resource_kind="skill",
        adapter="skill_markdown",
        locator="SKILL.md#L1",
        entry_index=0,
    )


def _entry(resource_id: str, title: str = "Craft details", body: str = "x" * 40) -> DesignEntry:
    return DesignEntry(
        entry_id=f"{resource_id}:0",
        kind="guidance",
        title=title,
        body=body,
        fields={"section": "typography", "note": "editorial scale"},
        provenance=_provenance(resource_id),
    )


def _available_report(resource_ids) -> DesignCapabilityReport:
    """A report marking the named resources AVAILABLE on this host."""
    resources = {
        rid: DesignCapability(
            resource_id=rid,
            kind="skill",
            required=(rid == GUIDANCE),
            configured=True,
            available=True,
            status=STATUS_AVAILABLE,
            detail="",
        )
        for rid in resource_ids
    }
    return DesignCapabilityReport(
        ok=True,
        manifest_version=1,
        profile_skills_dir="/tmp/profile/skills",
        resources=resources,
        failures=[],
        degraded=[],
    )


# ---------------------------------------------------------------------------
# Defect 2 — payload_chars accepts BOTH shapes
# ---------------------------------------------------------------------------

class PayloadCharsShapeTests(unittest.TestCase):
    def test_dataclass_entry_is_measured(self):
        entry = _entry("ui_ux_pro_max")
        self.assertGreater(payload_chars(entry), 0)

    def test_serialized_dict_entry_is_measured_identically(self):
        entry = _entry("ui_ux_pro_max")
        as_dict = entry.to_dict()
        self.assertEqual(payload_chars(entry), payload_chars(as_dict))

    def test_real_resource_result_to_dict_is_measurable(self):
        """The exact shape the pack stores must be measurable end to end."""
        result = DesignResourceResult(
            resource_id="ui_ux_pro_max",
            resource_kind="skill",
            status=STATUS_AVAILABLE,
            available=True,
            degraded=False,
            entries=(_entry("ui_ux_pro_max"), _entry("ui_ux_pro_max", title="Second")),
            warnings=(),
            truncated=False,
            dropped_entries=0,
        )
        pack = DesignContextPack(
            version=1,
            design_dna={},
            decisions={},
            resources={"ui_ux_pro_max": result.to_dict()},
            truncation={},
            limits={},
            degraded=False,
            warnings=(),
        )
        # This is the exact call that crashed with AttributeError before the fix.
        self.assertGreater(pack.total_chars, 0)
        self.assertIn("resources=1", pack.summary())

    def test_unknown_shape_raises_named_error(self):
        with self.assertRaises(DesignEntryError):
            payload_chars(object())  # type: ignore[arg-type]

    def test_missing_fields_raises_named_error(self):
        with self.assertRaises(DesignEntryError):
            payload_chars({"title": "t", "body": "b", "fields": None, "provenance": {}})

    def test_missing_provenance_raises_named_error(self):
        with self.assertRaises(DesignEntryError):
            payload_chars({"title": "t", "body": "b", "fields": {}, "provenance": None})


class IterEntriesGuardTests(unittest.TestCase):
    def test_iter_entries_yields_dict_entries(self):
        result = DesignResourceResult(
            resource_id="ui_ux_pro_max", resource_kind="skill",
            status=STATUS_AVAILABLE, available=True, degraded=False,
            entries=(_entry("ui_ux_pro_max"),), warnings=(),
            truncated=False, dropped_entries=0,
        )
        got = list(_iter_entries({"ui_ux_pro_max": result.to_dict()}))
        self.assertEqual(len(got), 1)
        self.assertIsInstance(got[0], dict)

    def test_iter_entries_rejects_non_mapping_resource(self):
        with self.assertRaises(DesignEntryError):
            list(_iter_entries({"bad": ["not", "a", "mapping"]}))

    def test_iter_entries_rejects_non_sequence_entries(self):
        with self.assertRaises(DesignEntryError):
            list(_iter_entries({"bad": {"entries": 12345}}))


# ---------------------------------------------------------------------------
# Defect 1 — capability_report drives reachability
# ---------------------------------------------------------------------------

class CapabilityWiringTests(unittest.TestCase):
    def _pack(self, *, capability_report, hermes_home):
        # A DNA that justifies the guidance skill, so selection is decided by
        # reachability alone (the variable under test).
        return build_design_context_pack(
            hermes_home,
            design_dna={"layout": "cards with buttons, forms and a dialog"},
            include_guidance=True,
            retrieve=False,  # decision-only: isolates reachability
            capability_report=capability_report,
        )

    def test_available_guidance_is_selected_when_report_present(self):
        report = _available_report([GUIDANCE])
        pack = self._pack(capability_report=report, hermes_home=Path("."))
        selected = pack.decisions["selected_resources"]
        self.assertIn(
            GUIDANCE,
            {e["resource_id"] for e in selected},
            "an AVAILABLE guidance skill must be selected when its capability "
            "report says it is reachable",
        )

    def test_without_report_guidance_is_not_selected(self):
        """No report => cannot prove reachability => honest non-selection."""
        pack = self._pack(capability_report=None, hermes_home=Path("."))
        selected = pack.decisions["selected_resources"]
        self.assertNotIn(GUIDANCE, {e["resource_id"] for e in selected})

    def test_unavailable_guidance_is_not_selected(self):
        report = DesignCapabilityReport(
            ok=True, manifest_version=1, profile_skills_dir="/tmp/profile/skills",
            resources={
                GUIDANCE: DesignCapability(
                    resource_id=GUIDANCE, kind="skill", required=True,
                    configured=True, available=False,
                    status="unavailable_required", detail="",
                )
            },
            failures=[], degraded=[],
        )
        pack = self._pack(capability_report=report, hermes_home=Path("."))
        selected = pack.decisions["selected_resources"]
        self.assertNotIn(
            GUIDANCE, {e["resource_id"] for e in selected},
            "an UNAVAILABLE resource must never be forced into the pack",
        )

    def test_report_does_not_fabricate_undeclared_resource(self):
        """A report naming an id outside the manifest changes nothing."""
        report = _available_report([GUIDANCE])
        report.resources["totally_made_up"] = DesignCapability(
            resource_id="totally_made_up", kind="skill", required=False,
            configured=True, available=True, status=STATUS_AVAILABLE, detail="",
        )
        pack = self._pack(capability_report=report, hermes_home=Path("."))
        all_ids = {e["resource_id"] for e in pack.decisions["selected_resources"]}
        self.assertNotIn("totally_made_up", all_ids)


class ManifestSanityTests(unittest.TestCase):
    def test_guidance_resource_is_declared(self):
        manifest = load_design_resource_manifest()
        self.assertEqual(manifest.get(GUIDANCE).kind, "skill")


# ---------------------------------------------------------------------------
# Defect 2c — the renderer bounds CONTENT, not pretty-print whitespace
# ---------------------------------------------------------------------------

class RenderKeepsContentWhenItFitsTests(unittest.TestCase):
    def _pack_with_real_entry(self):
        """A pack whose compact content fits well under the render ceiling."""
        result = DesignResourceResult(
            resource_id="ui_ux_pro_max",
            resource_kind="skill",
            status=STATUS_AVAILABLE,
            available=True,
            degraded=False,
            entries=(
                DesignEntry(
                    entry_id="ui_ux_pro_max:0",
                    kind="guidance",
                    title="Minimalism & Swiss Style",
                    body="",
                    fields={
                        "Keywords": "Clean, simple, spacious, functional, white space",
                        "Primary Colors": "Black #000000, White #FFFFFF",
                        "Best For": "Enterprise, editorial, portfolio",
                    },
                    provenance=_provenance("ui_ux_pro_max"),
                ),
            ),
            warnings=(),
            truncated=False,
            dropped_entries=0,
        )
        return DesignContextPack(
            version=1, design_dna={}, decisions={"selected_resources": []},
            resources={"ui_ux_pro_max": result.to_dict()},
            truncation={}, limits={}, degraded=False, warnings=(),
        )

    def test_a_fitting_pack_keeps_its_resources(self):
        from app.core.design_context_render import (
            MAX_RENDERED_CHARS,
            render_design_context_block,
        )

        pack = self._pack_with_real_entry()
        block = render_design_context_block(pack)

        # The real content must be present, not just the decision skeleton.
        self.assertIn("Minimalism & Swiss Style", block)
        self.assertIn("Clean, simple, spacious", block)
        self.assertNotIn(
            "rendered_truncated", block,
            "a pack whose content fits must NOT be trimmed to an empty resources map",
        )
        self.assertLessEqual(len(block), MAX_RENDERED_CHARS + 2_000)

    def test_an_oversized_pack_still_drops_resources_but_keeps_decisions(self):
        from app.core.design_context_render import render_design_context_block

        huge = {
            "ui_ux_pro_max": {
                "resource_id": "ui_ux_pro_max",
                "entries": [
                    {
                        "entry_id": f"x{i}", "kind": "guidance",
                        "title": "t" * 400, "body": "b" * 2000,
                        "fields": {"k": "v" * 400},
                        "provenance": {"resource_id": "ui_ux_pro_max",
                                       "resource_kind": "skill", "adapter": "guidance",
                                       "locator": "data/styles.csv", "entry_index": i},
                        "truncated": False,
                    }
                    for i in range(50)
                ],
                "warnings": [], "truncated": True, "dropped_entries": 0,
            }
        }

        class _HugePack:
            def to_dict(self):
                return {
                    "version": 1, "design_dna": {},
                    "decisions": {"selected_resources": [{"resource_id": "shadcn"}]},
                    "resources": huge, "truncation": {}, "limits": {},
                    "degraded": False, "warnings": [],
                }

        block = render_design_context_block(_HugePack())
        self.assertIn("shadcn", block, "decisions must survive the render bound")
        self.assertIn("rendered_truncated", block)
        self.assertLess(len(block), 20_000)


if __name__ == "__main__":
    unittest.main()
