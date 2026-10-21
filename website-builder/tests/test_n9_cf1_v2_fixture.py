"""CF-1 v2 -- the repaired fixture and its independent compliance criteria.

The previous CF-1 had a matched candidate whose cards read "Proyek Satu
placeholder". A page that labels its own projects as placeholders has not
delivered "3 proyek arsitektur", so the matched candidate was not matched,
and the pre-registered verdict of "satisfied" was wrong.

These tests pin the repaired fixture and the criteria, so neither can drift
without a failure.
"""
import json
import sys
from pathlib import Path

import pytest

FIX = Path("/home/albertus527/n8-benchmark/v3/fixtures/cf-1-counterfactual-v2")
sys.path.insert(0, str(FIX))

import compliance_criteria as cc  # noqa: E402

MATRIX = json.loads((FIX / "compliance-matrix.json").read_text())
CAPS = json.loads((FIX / "captures.json").read_text())


class TestTheCriteria:
    def test_three_real_projects_are_satisfied(self):
        cards = [
            {"title": "Rumah Batu",
             "meta": "Rumah tinggal, Bandung, 2023",
             "note": "Renovasi rumah bersejarah dengan halaman terbuka."},
            {"title": "Museum Rupa",
             "meta": "Museum, Yogyakarta, 2022",
             "note": "Pavilion seni untuk karya lokal."},
            {"title": "Kantor Citra",
             "meta": "Gedung perkantoran, Surabaya, 2024",
             "note": "Bangunan enam lantai dengan taman atap."},
        ]
        assert cc.classify_cards(cards)["verdict"] == "satisfied"

    def test_three_non_projects_are_absent(self):
        cards = [
            {"title": "Album Senja", "meta": "Album indie, Jakarta, 2023",
             "note": "Kumpulan lagu."},
            {"title": "Menu Pagi", "meta": "Menu sarapan, Yogyakarta, 2022",
             "note": "Roti artisan."},
            {"title": "Jadwal Kelas", "meta": "Jadwal workshop, Surabaya, 2024",
             "note": "Kursus fotografi."},
        ]
        assert cc.classify_cards(cards)["verdict"] == "absent"

    def test_a_mix_is_partial(self):
        cards = [
            {"title": "Rumah Batu", "meta": "Rumah, Bandung", "note": "Rumah."},
            {"title": "Album Senja", "meta": "Album", "note": "Album."},
            {"title": "Museum Rupa", "meta": "Museum, Yogyakarta",
             "note": "Pavilion seni."},
        ]
        assert cc.classify_cards(cards)["verdict"] == "partial"

    def test_placeholders_are_never_satisfied(self):
        """The defect that made the previous CF-1 invalid."""
        cards = [
            {"title": "Proyek Satu placeholder",
             "meta": "Placeholder -- nama proyek",
             "note": "Deskripsi singkat proyek belum tersedia."},
        ] * 3
        assert cc.classify_cards(cards)["verdict"] != "satisfied"

    def test_four_projects_are_partial(self):
        """The requirement says THREE."""
        cards = [
            {"title": f"Rumah {n}", "meta": "Rumah, Bandung", "note": "Rumah."}
            for n in range(4)
        ]
        assert cc.classify_cards(cards)["verdict"] == "partial"

    def test_no_cards_is_unverified(self):
        assert cc.classify_cards([])["verdict"] == "unverified"

    def test_an_absent_section_is_unverified(self):
        result = cc.classify_page({
            "cards": [], "section_present": False, "legible": True})
        assert result["verdict"] == "unverified"

    def test_illegible_capture_is_unverified(self):
        result = cc.classify_page({
            "cards": [{"title": "x", "meta": "y", "note": "z"}],
            "section_present": True, "legible": False})
        assert result["verdict"] == "unverified"


class TestTheFixture:
    def test_c1_is_satisfied_and_c2_is_absent(self):
        verdicts = {m["candidate"]: m["verdict"]
                    for m in MATRIX["matrix"]}
        assert verdicts["C1"] == "satisfied"
        assert verdicts["C2"] == "absent"

    def test_the_verdicts_come_from_the_render(self):
        """Recompute them from the capture and compare."""
        for label in ("C1", "C2"):
            dom = CAPS[label]["dom"]["desktop"]
            cards = [{"title": c["title"], "meta": c["meta"],
                      "note": c["note"]} for c in dom["cards"]]
            recomputed = cc.classify_cards(cards)["verdict"]
            recorded = next(m["verdict"] for m in MATRIX["matrix"]
                            if m["candidate"] == label)
            assert recomputed == recorded

    def test_neither_candidate_carries_a_placeholder_label(self):
        for label in ("C1", "C2"):
            dom = CAPS[label]["dom"]["desktop"]
            for card in dom["cards"]:
                blob = " ".join(card["all"]).lower()
                assert "placeholder" not in blob, (label, card["all"])

    def test_c1_names_three_architecture_projects(self):
        dom = CAPS["C1"]["dom"]["desktop"]
        titles = [c["title"] for c in dom["cards"]]
        assert titles == ["Rumah Batu", "Museum Rupa", "Kantor Citra"]

    def test_c2_names_three_non_architecture_entries(self):
        dom = CAPS["C2"]["dom"]["desktop"]
        titles = [c["title"] for c in dom["cards"]]
        assert titles == ["Album Senja", "Menu Pagi", "Jadwal Kelas"]
        for card in dom["cards"]:
            blob = " ".join(card["all"]).lower()
            assert not any(term in blob for term in
                           ("arsitektur", "rumah", "museum", "gedung",
                            "paviliun", "renovasi"))


class TestTheVisualControls:
    def test_the_card_geometry_is_identical(self):
        c1 = CAPS["C1"]["dom"]["desktop"]
        c2 = CAPS["C2"]["dom"]["desktop"]
        assert c1["articles"] == c2["articles"] == 3
        assert [b["w"] for b in c1["boxes"]] == [b["w"] for b in c2["boxes"]]
        assert [b["h"] for b in c1["boxes"]] == [b["h"] for b in c2["boxes"]]
        assert [b["y"] for b in c1["boxes"]] == [b["y"] for b in c2["boxes"]]

    def test_the_type_scale_is_identical(self):
        c1 = CAPS["C1"]["dom"]["desktop"]
        c2 = CAPS["C2"]["dom"]["desktop"]
        assert c1["font_sizes"] == c2["font_sizes"]

    def test_the_palette_is_identical(self):
        c1 = CAPS["C1"]["dom"]["desktop"]
        c2 = CAPS["C2"]["dom"]["desktop"]
        assert c1["text_colors"] == c2["text_colors"]

    def test_the_font_is_identical(self):
        assert (CAPS["C1"]["dom"]["desktop"]["font"]
                == CAPS["C2"]["dom"]["desktop"]["font"])

    def test_the_body_text_length_is_comparable(self):
        """Text density is a control, so a big length gap would be a
        confound."""
        c1 = CAPS["C1"]["dom"]["desktop"]["body_len"]
        c2 = CAPS["C2"]["dom"]["desktop"]["body_len"]
        assert abs(c1 - c2) / max(c1, c2) < 0.10, (c1, c2)

    def test_the_screenshot_digests_are_bound(self):
        for label in ("C1", "C2"):
            digests = CAPS[label]["screenshot_digests"]
            assert len(digests["desktop"]) == 64
            assert len(digests["mobile"]) == 64
            assert digests["desktop"] != digests["mobile"]


class TestTheHistoricalEvidenceIsPreserved:
    def test_cf1_v1_still_exists_with_its_run_and_its_correction(self):
        v1 = FIX.parent / "cf-1-counterfactual"
        assert (v1 / "live-result.json").exists(), (
            "the previous CF-1 run must be preserved")
        assert (v1 / "compliance-matrix.json").exists()
        result = json.loads((v1 / "live-result.json").read_text())
        assert result["ok"] is True
        assert result["answer_count"] == 16

    def test_v1_and_v2_are_separate_fixtures(self):
        v1 = FIX.parent / "cf-1-counterfactual"
        assert v1 != FIX
        assert (v1 / "captures.json").exists()
        assert (FIX / "captures.json").exists()
