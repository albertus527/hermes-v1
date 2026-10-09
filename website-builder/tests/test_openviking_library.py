"""D4a: OpenViking context-library schema + ingestion (offline, deterministic).

Pins the load-bearing properties of the foundation a later batch (D4b, Laya)
consumes:

    * the category/trust/level vocabularies are CLOSED;
    * the Viking-URI isolation predicate is exact (no prefix, traversal, or
      scheme confusion);
    * ingestion is allowlisted, bounded, idempotent, and refuses secrets,
      logs, dependency folders, and executable content;
    * provenance (locator, revision, digest, trust, scope) is preserved on
      every indexed resource;
    * the module performs no dependency install and no subprocess execution.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import openviking_library as lib


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _reader_for(files):
    def reader(locator):
        return files.get(locator)

    return reader


def _source(source_id="s1", project_id="alpha", category="design_dna",
            trust="reviewed", locator="skills/x/a.md", content_type="text/markdown"):
    return lib.SourceSpec(
        source_id=source_id, project_id=project_id, category=category,
        trust=trust, locator=locator, content_type=content_type,
    )


# ---------------------------------------------------------------------------
# Closed vocabularies
# ---------------------------------------------------------------------------


def test_the_category_vocabulary_is_closed():
    assert set(lib.CATEGORIES) == {
        "design_dna", "components", "motion", "briefs", "decisions",
    }


def test_the_trust_vocabulary_is_closed_and_ordered():
    assert lib.TRUST_LEVELS == ("reviewed", "internal", "external")
    # reviewed is the MOST trusted (lowest rank).
    assert lib.TRUST_RANK["reviewed"] < lib.TRUST_RANK["internal"] < lib.TRUST_RANK["external"]


def test_the_level_vocabulary_matches_openviking_l0_l1_l2():
    assert lib.LEVELS == (0, 1, 2)
    assert (lib.LEVEL_ABSTRACT, lib.LEVEL_OVERVIEW, lib.LEVEL_DETAIL) == (0, 1, 2)


def test_the_schema_version_is_declared():
    assert lib.LIBRARY_SCHEMA_VERSION >= 1


def test_the_pinned_openviking_version_is_recorded():
    # A fact, not an auto-install: nothing here installs it.
    assert lib.OPENVIKING_PINNED_VERSION == "0.4.23"


# ---------------------------------------------------------------------------
# Project id + URI construction
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "Alpha", "a/b", "../x", "a b", "-x", "x" * 65, None, 7])
def test_an_invalid_project_id_is_refused(bad):
    assert lib.is_valid_project_id(bad) is False
    with pytest.raises(ValueError):
        lib.project_root_uri(bad)


@pytest.mark.parametrize("good", ["alpha", "p1", "proj-1", "a_b_c", "0abc"])
def test_a_valid_project_id_is_accepted(good):
    assert lib.is_valid_project_id(good) is True


def test_resource_uri_is_deterministic_and_scoped():
    uri = lib.resource_uri("alpha", "design_dna", "Refero Typography")
    assert uri == "viking://resources/website-builder/projects/alpha/design_dna/refero_typography"
    # The same inputs always produce the same URI (idempotent identity).
    assert lib.resource_uri("alpha", "design_dna", "Refero Typography") == uri


def test_an_unknown_category_is_refused():
    with pytest.raises(ValueError):
        lib.category_uri("alpha", "not_a_category")
    with pytest.raises(ValueError):
        lib.resource_uri("alpha", "not_a_category", "x")


# ---------------------------------------------------------------------------
# The isolation predicate (the load-bearing cross-project guarantee)
# ---------------------------------------------------------------------------


def test_a_uri_inside_the_scope_is_accepted():
    root = lib.project_root_uri("alpha")
    assert lib.is_uri_within_scope(root, root) is True
    assert lib.is_uri_within_scope(root + "/design_dna/a", root) is True
    assert lib.is_uri_within_scope(root + "/", root) is True


def test_a_uri_for_a_DIFFERENT_project_is_refused():
    """The central isolation guarantee: project 'ab' is NOT inside 'a'."""
    scope = lib.project_root_uri("a")
    assert lib.is_uri_within_scope(lib.project_root_uri("ab") + "/x", scope) is False
    assert lib.is_uri_within_scope(lib.project_root_uri("b") + "/x", scope) is False


def test_a_prefix_lookalike_is_not_inside_the_scope():
    scope = "viking://resources/website-builder/projects/alpha"
    # A sibling directory whose NAME merely starts with the scope string.
    assert lib.is_uri_within_scope(scope + "x/y", scope) is False


@pytest.mark.parametrize("bad", [
    "viking://resources/website-builder/projects/alpha/../../etc/passwd",
    "viking://resources/website-builder/projects/alpha/./x",
    "http://localhost:1933/x",
    "file:///etc/passwd",
    "resources/website-builder/projects/alpha/x",
    "",
    None,
    7,
])
def test_a_malformed_or_traversing_uri_is_refused(bad):
    assert lib.is_uri_within_scope(bad, lib.project_root_uri("alpha")) is False


# ---------------------------------------------------------------------------
# Forbidden sources
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("locator", [
    "repo/.env",
    "repo/.env.local",
    "secrets/app.env",
    "home/.ssh/id_rsa",
    "proj/node_modules/react/index.js",
    "proj/.git/config",
    "proj/__pycache__/x.pyc",
    "logs/run.log",
    "certs/server.pem",
    "bin/tool.exe",
])
def test_a_forbidden_source_is_refused(locator):
    assert lib.is_forbidden_source(locator) is True


@pytest.mark.parametrize("locator", [
    "skills/refero-design/references/typography.md",
    "config/design_resources.yaml",
    "briefs/brief.md",
])
def test_an_allowed_source_is_accepted(locator):
    assert lib.is_forbidden_source(locator) is False


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------


def test_ingest_indexes_an_allowlisted_source_with_full_provenance():
    backend = lib.FakeOpenVikingBackend()
    content = b"# Typography\nLine length 60-75 characters."
    src = _source(source_id="refero_typography", locator="skills/refero/references/typography.md")
    report = lib.ingest_sources(
        backend, [src], reader=_reader_for({src.locator: content}),
        project_id="alpha", clock="2026-01-01T00:00:00Z",
    )
    assert report.statuses == {"indexed": 1}
    item = report.items[0]
    assert item.indexed is True
    record = item.record
    # Every provenance field is present.
    assert record.source_id == "refero_typography"
    assert record.canonical_locator == "skills/refero/references/typography.md"
    assert record.source_revision == lib.source_revision(content)
    assert record.project_id == "alpha"
    assert record.category == "design_dna"
    assert record.trust == "reviewed"
    assert record.content_type == "text/markdown"
    assert record.ingested_at == "2026-01-01T00:00:00Z"
    assert record.digest == lib.source_digest(content)
    assert record.byte_size == len(content)
    assert record.uri == src.uri


def test_ingest_is_idempotent_for_an_unchanged_source():
    backend = lib.FakeOpenVikingBackend()
    content = b"# Color\nUse a 60/30/10 split."
    src = _source(source_id="refero_color", locator="skills/refero/references/color.md")
    reader = _reader_for({src.locator: content})
    first = lib.ingest_sources(backend, [src], reader=reader, project_id="alpha")
    second = lib.ingest_sources(backend, [src], reader=reader, project_id="alpha")
    assert first.statuses == {"indexed": 1}
    assert second.statuses == {"skipped_duplicate": 1}
    assert second.total_bytes == 0


def test_reingesting_a_CHANGED_source_advances_the_revision():
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="brief", locator="briefs/brief.md")
    v1 = b"brief v1"
    v2 = b"brief v2 -- accepted requirements changed"
    lib.ingest_sources(backend, [src], reader=_reader_for({src.locator: v1}), project_id="alpha")
    report = lib.ingest_sources(
        backend, [src], reader=_reader_for({src.locator: v2}), project_id="alpha",
    )
    assert report.statuses == {"indexed": 1}
    record = backend.get_record("alpha", src.uri)
    assert record.source_revision == lib.source_revision(v2)
    assert record.source_revision != lib.source_revision(v1)


def test_a_source_for_another_project_is_rejected_not_rescoped():
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="x", project_id="beta", locator="briefs/b.md")
    report = lib.ingest_sources(
        backend, [src], reader=_reader_for({src.locator: b"data"}), project_id="alpha",
    )
    assert report.statuses == {"rejected_allowlist": 1}
    # Nothing was written for alpha OR beta.
    assert backend.get_record("alpha", src.uri) is None


def test_a_forbidden_source_is_skipped_before_any_read():
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="env", locator="repo/.env")
    read_calls = []

    def reader(locator):
        read_calls.append(locator)
        return b"SECRET=1"

    report = lib.ingest_sources(backend, [src], reader=reader, project_id="alpha")
    assert report.statuses == {"skipped_forbidden": 1}
    assert read_calls == []  # never read


def test_an_oversize_source_is_refused():
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="big", locator="briefs/big.md")
    content = b"x" * (lib.MAX_SOURCE_BYTES + 1)
    report = lib.ingest_sources(
        backend, [src], reader=_reader_for({src.locator: content}), project_id="alpha",
    )
    assert report.statuses == {"rejected_oversize": 1}


def test_an_unreadable_source_is_reported_not_invented():
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="missing", locator="briefs/missing.md")
    report = lib.ingest_sources(
        backend, [src], reader=_reader_for({}), project_id="alpha",
    )
    assert report.statuses == {"rejected_unreadable": 1}


def test_ingestion_is_bounded_by_the_resource_limit():
    backend = lib.FakeOpenVikingBackend()
    sources = [
        _source(source_id=f"s{i}", locator=f"briefs/b{i}.md")
        for i in range(lib.MAX_INGEST_RESOURCES + 5)
    ]
    files = {s.locator: b"content" for s in sources}
    report = lib.ingest_sources(
        backend, sources, reader=_reader_for(files), project_id="alpha",
    )
    assert report.truncated is True
    assert len(report.items) == lib.MAX_INGEST_RESOURCES


def test_ingestion_failure_is_isolated_per_source():
    """One failing write does not abort the batch."""
    backend = lib.FakeOpenVikingBackend()
    good = _source(source_id="good", locator="briefs/good.md")
    bad = _source(source_id="bad", locator="briefs/bad.md")
    files = {good.locator: b"ok", bad.locator: b"also ok"}

    original = backend.put_resource

    def flaky(project_id, uri, content, record):
        if record.source_id == "bad":
            raise RuntimeError("backend write failed")
        return original(project_id, uri, content, record)

    backend.put_resource = flaky  # type: ignore[assignment]
    report = lib.ingest_sources(
        backend, [good, bad], reader=_reader_for(files), project_id="alpha",
    )
    assert report.statuses == {"indexed": 1, "error": 1}


def test_the_ingest_summary_is_payload_free():
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="s", locator="briefs/b.md")
    report = lib.ingest_sources(
        backend, [src], reader=_reader_for({src.locator: b"secret-looking-body"}),
        project_id="alpha",
    )
    summary = report.summary()
    assert "secret-looking-body" not in summary
    assert "alpha" in summary


# ---------------------------------------------------------------------------
# Credential-shape detection (value-free)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "OPENAI_API_KEY=sk-proj-aaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "-----BEGIN RSA PRIVATE KEY-----\nMIIE...",
    "AWS_ACCESS_KEY_ID=AKIAIOSFODNN7EXAMPLE",
    "token: ghp_0123456789abcdefghijklmnopqrstuvwxyz",
    "PASSWORD=hunter2hunter2hunter2",
])
def test_credential_shaped_text_is_detected(text):
    assert lib.text_looks_like_credential(text) is True


@pytest.mark.parametrize("text", [
    "Use a modular type scale of 1.25.",
    "Line length should be 60-75 characters.",
    "",
    "The colour contrast ratio must reach 4.5:1.",
])
def test_ordinary_guidance_is_not_flagged(text):
    assert lib.text_looks_like_credential(text) is False


def test_a_long_uri_alongside_the_word_token_is_not_flagged():
    """Regression (D4a.1): a design doc that lists long URIs AND mentions
    "design tokens" must not be misread as a credential. The real OpenViking L1
    overview sidecars list ``viking://resources/…`` paths, and "tokens" appears
    throughout design prose; the entropy heuristic previously fired on the URI
    blob."""
    text = (
        "Directory: viking://resources/website-builder/projects/wb-design/"
        "design_dna/refero_typography\n"
        "Covers design tokens, type scale, and color tokens.\n"
    )
    assert lib.text_looks_like_credential(text) is False


def test_a_real_opaque_secret_blob_IS_still_flagged():
    """The false-positive fix must not blind the detector: a mixed-case,
    digit-bearing opaque blob next to a key word is still caught."""
    text = "api_key sk-proj-AbCdEf0123456789AbCdEf0123456789zz"
    assert lib.text_looks_like_credential(text) is True
    text2 = "bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9AbCdEf0123456789"
    assert lib.text_looks_like_credential(text2) is True


# ---------------------------------------------------------------------------
# No dependency or toolchain mutation
# ---------------------------------------------------------------------------


def test_the_module_performs_no_subprocess_or_install():
    """The foundation is pure Python: no shell, no installer, no npm."""
    source = (Path(__file__).resolve().parents[1] / "app" / "core" / "openviking_library.py").read_text(
        encoding="utf-8"
    )
    for forbidden in ("subprocess", "os.system", "os.popen", "pip install", "npm ", "yarn "):
        assert forbidden not in source, f"unexpected {forbidden!r} in openviking_library"


def test_ingestion_only_ever_writes_through_the_backend():
    """Ingestion cannot touch the real filesystem: its only sink is the backend."""
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="s", locator="briefs/b.md")
    before = set(backend._store)
    lib.ingest_sources(
        backend, [src], reader=_reader_for({src.locator: b"body"}), project_id="alpha",
    )
    assert set(backend._store) - before == {src.uri}


def test_source_spec_rejects_an_invalid_category_or_trust():
    with pytest.raises(ValueError):
        lib.SourceSpec(source_id="s", project_id="alpha", category="nope",
                       trust="reviewed", locator="a/b.md")
    with pytest.raises(ValueError):
        lib.SourceSpec(source_id="s", project_id="alpha", category="design_dna",
                       trust="super", locator="a/b.md")


def test_the_record_round_trips_through_a_dict():
    backend = lib.FakeOpenVikingBackend()
    src = _source(source_id="s", locator="briefs/b.md")
    report = lib.ingest_sources(
        backend, [src], reader=_reader_for({src.locator: b"body"}), project_id="alpha",
        clock="2026-01-01T00:00:00Z",
    )
    record = report.items[0].record
    assert lib.ResourceRecord.from_dict(record.to_dict()) == record
