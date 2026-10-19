"""N8 B3: visual reference ingestion, storage and evidence integrity.

Proves the things the owner required:

* references are backed by GENUINE assets (magic bytes decide, not the manifest);
* binary storage and text indexing stay separate, with separate size ceilings;
* asset digests and source revisions are bound, never inferred;
* rights are explicit -- an owner upload alone is not proof;
* E2/E3 cannot be elevated from unverified metadata.
"""
from __future__ import annotations

import json
import struct
import sys
import zlib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import design_reference as dr  # noqa: E402
from app.core import openviking_library as lib  # noqa: E402
from app.core import openviking_references as oref  # noqa: E402

PROJECT = "proj-1"


def _png(w=4, h=4) -> bytes:
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"\x00" * (w * 3)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw))
            + chunk(b"IEND", b""))


def _jpeg() -> bytes:
    return b"\xff\xd8\xff\xe0" + b"\x00" * 40


def _webp() -> bytes:
    return b"RIFF" + struct.pack("<I", 20) + b"WEBP" + b"\x00" * 20


def _reader(files):
    return lambda locator: files[locator]


def _manifest(**over):
    png = _png()
    base = dict(
        reference_id="ref-1",
        project_id=PROJECT,
        source_uri="https://example.test/ref-1",
        source_revision="rev-abc",
        ingested_at="2026-10-18T00:00:00Z",
        trust=dr.TRUST_REVIEWED,
        rights_confirmed=True,
        assets=(
            oref.AssetSpec(kind=dr.ASSET_SCREENSHOT_DESKTOP,
                           locator="ref/desktop.png", mime="image/png",
                           width=1440, height=900,
                           digest=oref.sha256_hex(png)),
        ),
    )
    base.update(over)
    return oref.ReferenceManifest(**base)


# ---------------------------------------------------------------------------
# Category and limits stay separate
# ---------------------------------------------------------------------------


def test_references_category_exists():
    assert "references" in lib.CATEGORIES


def test_existing_categories_unchanged():
    for name in ("design_dna", "components", "motion", "briefs", "decisions"):
        assert name in lib.CATEGORIES


def test_asset_and_text_limits_are_separate():
    assert lib.MAX_ASSET_BYTES != lib.MAX_SOURCE_BYTES
    assert lib.MAX_MANIFEST_BYTES != lib.MAX_ASSET_BYTES
    assert lib.MAX_ASSET_BYTES > lib.MAX_SOURCE_BYTES


def test_manifest_limit_is_smaller_than_asset_limit():
    assert lib.MAX_MANIFEST_BYTES < lib.MAX_ASSET_BYTES


# ---------------------------------------------------------------------------
# Genuine assets: the bytes decide
# ---------------------------------------------------------------------------


def test_sniff_png():
    assert oref.sniff_mime(_png()) == "image/png"


def test_sniff_jpeg():
    assert oref.sniff_mime(_jpeg()) == "image/jpeg"


def test_sniff_webp():
    assert oref.sniff_mime(_webp()) == "image/webp"


def test_sniff_rejects_markdown():
    assert oref.sniff_mime(b"# Typography\n\nAim for 45 to 75 chars.") == ""


def test_sniff_rejects_empty():
    assert oref.sniff_mime(b"") == ""


def test_prepare_binds_real_digest():
    png = _png()
    prepared = oref.prepare_reference(
        _manifest(), reader=_reader({"ref/desktop.png": png}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    asset = prepared.reference.assets[0]
    assert asset.digest == oref.sha256_hex(png)
    assert asset.revision == "rev-abc"


def test_prepare_rejects_digest_mismatch():
    with pytest.raises(oref.ReferenceIngestError) as exc:
        oref.prepare_reference(
            _manifest(), reader=_reader({"ref/desktop.png": _png(8, 8)}),
            asset_base_uri="viking://p/ref/references/ref-1",
        )
    assert exc.value.status == oref.STATUS_REJECTED_DIGEST


def test_prepare_rejects_declared_mime_mismatch():
    """A manifest claiming PNG for JPEG bytes is a mismatch, not a coercion."""
    jpg = _jpeg()
    m = _manifest(assets=(oref.AssetSpec(
        kind=dr.ASSET_SCREENSHOT_DESKTOP, locator="ref/x", mime="image/png",
        width=10, height=10, digest=oref.sha256_hex(jpg)),))
    with pytest.raises(oref.ReferenceIngestError) as exc:
        oref.prepare_reference(m, reader=_reader({"ref/x": jpg}),
                               asset_base_uri="viking://p/ref/references/ref-1")
    assert exc.value.status == oref.STATUS_REJECTED_MIME


def test_prepare_rejects_non_image():
    text = b"# Typography\n\nAim for 45 to 75 characters."
    m = _manifest(assets=(oref.AssetSpec(
        kind=dr.ASSET_SCREENSHOT_DESKTOP, locator="ref/t.md", mime="image/png",
        width=10, height=10, digest=oref.sha256_hex(text)),))
    with pytest.raises(oref.ReferenceIngestError) as exc:
        oref.prepare_reference(m, reader=_reader({"ref/t.md": text}),
                               asset_base_uri="viking://p/ref/references/ref-1")
    assert exc.value.status == oref.STATUS_REJECTED_MIME


def test_prepare_rejects_oversize_asset():
    big = _png() + b"\x00" * lib.MAX_ASSET_BYTES
    m = _manifest(assets=(oref.AssetSpec(
        kind=dr.ASSET_SCREENSHOT_DESKTOP, locator="ref/big.png", mime="image/png",
        width=10, height=10, digest=""),))
    with pytest.raises(oref.ReferenceIngestError) as exc:
        oref.prepare_reference(m, reader=_reader({"ref/big.png": big}),
                               asset_base_uri="viking://p/ref/references/ref-1")
    assert exc.value.status == oref.STATUS_REJECTED_SIZE


# ---------------------------------------------------------------------------
# Rights
# ---------------------------------------------------------------------------


def test_upload_alone_is_not_rights():
    """Owner upload without a claim is not proof of usage rights."""
    m = _manifest(rights_confirmed=False, licence="", trust=dr.TRUST_EXTERNAL)
    assert m.has_rights is False
    with pytest.raises(oref.ReferenceIngestError) as exc:
        oref.prepare_reference(m, reader=_reader({"ref/desktop.png": _png()}),
                               asset_base_uri="viking://p/ref/references/ref-1")
    assert exc.value.status == oref.STATUS_REJECTED_RIGHTS


def test_confirmed_rights_are_accepted():
    assert _manifest(rights_confirmed=True).has_rights is True


def test_licence_plus_internal_trust_is_accepted():
    m = _manifest(rights_confirmed=False, licence="MIT", trust=dr.TRUST_INTERNAL)
    assert m.has_rights is True


def test_licence_alone_on_external_is_not_enough():
    """External material needs an explicit confirmation, not just a string."""
    m = _manifest(rights_confirmed=False, licence="MIT", trust=dr.TRUST_EXTERNAL)
    assert m.has_rights is False


# ---------------------------------------------------------------------------
# Manifest strictness
# ---------------------------------------------------------------------------


def test_manifest_requires_provenance():
    with pytest.raises(oref.ReferenceIngestError):
        _manifest(source_uri="")


def test_manifest_rejects_bad_project_id():
    with pytest.raises(oref.ReferenceIngestError) as exc:
        _manifest(project_id="../etc")
    assert exc.value.status == oref.STATUS_REJECTED_PROJECT


def test_manifest_rejects_forbidden_asset_path():
    with pytest.raises(oref.ReferenceIngestError) as exc:
        _manifest(assets=(oref.AssetSpec(
            kind=dr.ASSET_SCREENSHOT_DESKTOP, locator=".env", mime="image/png",
            width=1, height=1, digest=""),))
    assert exc.value.status == oref.STATUS_REJECTED_FORBIDDEN


def test_manifest_rejects_unknown_asset_kind():
    with pytest.raises(oref.ReferenceIngestError):
        _manifest(assets=(oref.AssetSpec(
            kind="hologram", locator="ref/x", mime="image/png",
            width=1, height=1, digest=""),))


def test_manifest_rejects_unsupported_mime():
    with pytest.raises(oref.ReferenceIngestError) as exc:
        _manifest(assets=(oref.AssetSpec(
            kind=dr.ASSET_SCREENSHOT_DESKTOP, locator="ref/x", mime="image/gif",
            width=1, height=1, digest=""),))
    assert exc.value.status == oref.STATUS_REJECTED_MIME


def test_manifest_rejects_no_assets_at_prepare():
    with pytest.raises(oref.ReferenceIngestError):
        oref.prepare_reference(_manifest(assets=()), reader=_reader({}),
                               asset_base_uri="viking://p/ref/references/ref-1")


def test_parse_manifest_round_trip():
    m = _manifest()
    data = {
        "reference_id": m.reference_id, "project_id": m.project_id,
        "source_uri": m.source_uri, "source_revision": m.source_revision,
        "ingested_at": m.ingested_at, "trust": m.trust,
        "rights_confirmed": True, "licence": "",
        "assets": [{"kind": a.kind, "locator": a.locator, "mime": a.mime,
                    "width": a.width, "height": a.height, "digest": a.digest}
                   for a in m.assets],
        "schema_version": oref.MANIFEST_SCHEMA_VERSION,
    }
    again = oref.parse_manifest(data)
    assert again.reference_id == m.reference_id
    assert again.assets[0].digest == m.assets[0].digest


def test_parse_manifest_rejects_future_version():
    with pytest.raises(oref.ReferenceIngestError):
        oref.parse_manifest({"schema_version": 99})


# ---------------------------------------------------------------------------
# Evidence integrity: metadata cannot elevate a class
# ---------------------------------------------------------------------------


def test_asset_only_yields_rendered_evidence():
    prepared = oref.prepare_reference(
        _manifest(), reader=_reader({"ref/desktop.png": _png()}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    ref = prepared.reference
    assert ref.evidence_for(dr.DIMENSION_HIERARCHY) == dr.EVIDENCE_RENDERED
    assert ref.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_RENDERED


def test_declared_metadata_cannot_claim_structural():
    """A manifest may DECLARE contrast; only a measurement yields E3."""
    prepared = oref.prepare_reference(
        _manifest(declared=(("contrast", "21:1"), ("fonts", "Inter")),
                  extracted=()),
        reader=_reader({"ref/desktop.png": _png()}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    assert prepared.reference.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_RENDERED
    assert prepared.reference.evidence_for(dr.DIMENSION_TYPOGRAPHY) == dr.EVIDENCE_RENDERED


def test_measured_structural_yields_e3():
    prepared = oref.prepare_reference(
        _manifest(structural=dr.StructuralFacts(
            viewport=(1440, 900),
            font_families=("Inter",),
            type_scale=(("h1", 48.0, 52.0),),
            palette_pairs=(("#fff", "#000", 17.2),),
        )),
        reader=_reader({"ref/desktop.png": _png()}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    assert prepared.reference.evidence_for(dr.DIMENSION_COLOR) == dr.EVIDENCE_STRUCTURAL
    assert prepared.reference.evidence_for(dr.DIMENSION_TYPOGRAPHY) == dr.EVIDENCE_STRUCTURAL


def test_no_asset_means_no_visual_evidence():
    """A reference with only declared metadata has E0 for every dimension."""
    ref = dr.DesignReference(
        reference_id="r", source_uri="u", source_revision="rev",
        ingested_at="t", declared=(("screenshot", "yes"),),
    )
    for dim in dr.DIMENSIONS:
        assert ref.evidence_for(dim) == dr.EVIDENCE_NONE


# ---------------------------------------------------------------------------
# Binary / text separation
# ---------------------------------------------------------------------------


def test_manifest_bytes_are_text():
    prepared = oref.prepare_reference(
        _manifest(), reader=_reader({"ref/desktop.png": _png()}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    assert prepared.manifest_bytes.startswith(b"{")
    json.loads(prepared.manifest_bytes)  # must parse


def test_asset_bytes_are_stored_verbatim():
    png = _png()
    prepared = oref.prepare_reference(
        _manifest(), reader=_reader({"ref/desktop.png": png}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    uri, raw, sidecar = prepared.assets[0]
    assert raw == png
    assert sidecar["digest"] == oref.sha256_hex(png)


def test_manifest_does_not_embed_asset_bytes():
    png = _png()
    prepared = oref.prepare_reference(
        _manifest(), reader=_reader({"ref/desktop.png": png}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    assert png not in prepared.manifest_bytes


def test_two_assets_have_distinct_uris():
    png = _png()
    m = _manifest(assets=(
        oref.AssetSpec(dr.ASSET_SCREENSHOT_DESKTOP, "ref/d.png", "image/png",
                       1440, 900, oref.sha256_hex(png)),
        oref.AssetSpec(dr.ASSET_SCREENSHOT_MOBILE, "ref/m.png", "image/png",
                       390, 844, oref.sha256_hex(png)),
    ))
    prepared = oref.prepare_reference(
        m, reader=_reader({"ref/d.png": png, "ref/m.png": png}),
        asset_base_uri="viking://p/ref/references/ref-1",
    )
    uris = [a.uri for a in prepared.reference.assets]
    assert len(set(uris)) == 2
    assert prepared.reference.viewports == (dr.ASSET_SCREENSHOT_DESKTOP,
                                            dr.ASSET_SCREENSHOT_MOBILE)


def test_all_or_nothing_on_one_bad_asset():
    """A broken second asset rejects the whole reference."""
    png = _png()
    m = _manifest(assets=(
        oref.AssetSpec(dr.ASSET_SCREENSHOT_DESKTOP, "ref/d.png", "image/png",
                       1440, 900, oref.sha256_hex(png)),
        oref.AssetSpec(dr.ASSET_SCREENSHOT_MOBILE, "ref/bad.png", "image/png",
                       390, 844, ""),
    ))
    with pytest.raises(oref.ReferenceIngestError):
        oref.prepare_reference(
            m, reader=_reader({"ref/d.png": png, "ref/bad.png": b"not an image"}),
            asset_base_uri="viking://p/ref/references/ref-1",
        )
