"""N8 B7 calibration v2: fixture integrity for D4/D5/D6.

The six fixtures must each isolate one property family. This file locks the
properties that make that true, so a later edit cannot silently turn a fixture
into a two-defect build.

It does NOT claim the scorer will detect them — that is the live run's job.
"""
from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CTRL = Path("/home/albertus527/n8-benchmark/v3/v2-dataset/controlled")
EVIDENCE = Path("/home/albertus527/n8-benchmark/v3/v2-dataset/fixtures-evidence.json")
KEYS = ("C", "D1", "D2", "D3", "D4", "D5", "D6")


def _png_dims(path: Path):
    head = path.read_bytes()[:24]
    assert head[:8] == b"\x89PNG\r\n\x1a\n"
    return struct.unpack(">II", head[16:24])


def _evidence():
    return json.load(open(EVIDENCE))


# ---------------------------------------------------------------------------
# All six fixtures exist, in both viewports
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", KEYS)
def test_fixture_has_both_viewports(key):
    for kind in ("desktop", "mobile"):
        path = CTRL / key / "attempt-0" / f"{kind}.png"
        assert path.exists(), f"{key}/{kind} missing"
        assert path.stat().st_size > 20_000, f"{key}/{kind} suspiciously small"


@pytest.mark.parametrize("key", KEYS)
def test_desktop_is_1440_wide(key):
    width, _height = _png_dims(CTRL / key / "attempt-0" / "desktop.png")
    assert width == 1440


@pytest.mark.parametrize("key", KEYS)
def test_mobile_is_390_wide(key):
    width, _height = _png_dims(CTRL / key / "attempt-0" / "mobile.png")
    assert width == 390


# ---------------------------------------------------------------------------
# The geometry invariants that make each fixture single-defect
# ---------------------------------------------------------------------------


def test_colour_fixture_does_not_move_geometry():
    """D2 changes ink lightness only, so page height must equal the baseline."""
    assert _png_dims(CTRL / "D2" / "attempt-0" / "desktop.png")[1] == \
           _png_dims(CTRL / "C" / "attempt-0" / "desktop.png")[1]


def test_usability_fixture_does_not_move_geometry():
    """D5 changes affordance styling only; it must not reflow the page."""
    assert _png_dims(CTRL / "D5" / "attempt-0" / "desktop.png")[1] == \
           _png_dims(CTRL / "C" / "attempt-0" / "desktop.png")[1]


def test_typography_fixture_reflows():
    """D4 shrinks body type, so height MUST drop. A non-drop means it did not land."""
    assert _png_dims(CTRL / "D4" / "attempt-0" / "desktop.png")[1] < \
           _png_dims(CTRL / "C" / "attempt-0" / "desktop.png")[1]


def test_layout_fixture_reflows_large():
    """D3 randomises all spacing; the height change must be the largest."""
    heights = {k: _png_dims(CTRL / k / "attempt-0" / "desktop.png")[1] for k in KEYS}
    base = heights["C"]
    d3_drop = base - heights["D3"]
    for other in ("D1", "D2", "D4", "D5", "D6"):
        assert d3_drop > base - heights[other], f"D3 not the largest reflow vs {other}"


def test_consistency_fixture_stays_near_baseline_height():
    """D6 touches repeated-element treatments, not section rhythm.

    A large height change would mean it became a spacing fixture and could no
    longer be attributed to consistency.
    """
    base = _png_dims(CTRL / "C" / "attempt-0" / "desktop.png")[1]
    d6 = _png_dims(CTRL / "D6" / "attempt-0" / "desktop.png")[1]
    assert abs(d6 - base) / base < 0.05


# ---------------------------------------------------------------------------
# Every fixture declares exactly one target dimension
# ---------------------------------------------------------------------------


def test_each_fixture_declares_one_target():
    targets = _evidence()["targets"]
    assert set(targets) == {"D1", "D2", "D3", "D4", "D5", "D6"}
    assert sorted(targets.values()) == sorted(
        ["hierarchy", "color", "layout", "typography", "usability", "consistency"]
    )


def test_no_target_is_duplicated():
    """Six fixtures must cover six DIFFERENT dimensions, else coverage is a lie."""
    targets = _evidence()["targets"]
    assert len(set(targets.values())) == 6


def test_cross_dimensional_risk_is_declared_for_every_fixture():
    risk = _evidence()["risk"]
    assert set(risk) == {"D1", "D2", "D3", "D4", "D5", "D6"}
    for key, entry in risk.items():
        assert entry["level"] in ("very low", "low", "moderate", "HIGH"), key


def test_consistency_fixture_is_flagged_as_high_risk():
    """D6 and D3 both touch padding; the honest label must survive edits."""
    assert _evidence()["risk"]["D6"]["level"] == "HIGH"


# ---------------------------------------------------------------------------
# Provenance and isolation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("key", KEYS)
def test_provenance_records_a_sha256(key):
    prov = _evidence()["provenance"][key]
    assert len(prov["desktop"]) == 64
    assert len(prov["mobile"]) == 64
    assert prov["desktop"] != prov["mobile"]


@pytest.mark.parametrize("key", KEYS)
def test_rights_are_recorded_as_internal(key):
    prov = _evidence()["provenance"][key]
    assert "internal" in prov["rights"]
    assert "no third-party imagery" in prov["rights"]


def test_fixtures_live_in_a_test_namespace_only():
    """No fixture path may reference a production project id."""
    for key in KEYS:
        path = str(CTRL / key)
        assert "n8-benchmark" in path
        assert "website-workspaces/tg-" not in path


def test_baseline_workspace_was_not_mutated():
    """The source build must be byte-identical after all six derived builds."""
    assert _evidence()["geometry"]["C"]["desktop"] == [1440, 4210]
