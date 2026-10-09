#!/usr/bin/env python3
"""D4a.1 LIVE QUALIFICATION runner -- one command, real server, real evidence.

This is the operator command that runs AFTER the mandatory approval checkpoint
and AFTER the OpenViking server is provisioned (see
``docs/D4A1_OPENVIKING_OPERATIONS.md``). It exercises the REAL server through the
PRODUCTION D4a adapter -- no fake backend, no HTTP mock -- and writes a
secret-free JSON evidence file.

It refuses to run against a mock: it requires a live ``/health`` response from
the configured base URL, and it refuses a non-loopback base URL unless
``--allow-remote`` is given.

    python tools/openviking_qualify.py \
        --base-url http://127.0.0.1:1933 \
        --profile-skills-dir ~/.hermes-website/skills \
        --project-id wb-design \
        --out ~/.website-builder/openviking/qualification.json

The API key is read from ``OPENVIKING_API_KEY`` (never a flag, never logged).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import openviking_library as lib  # noqa: E402
from app.core import openviking_retrieval as ovr  # noqa: E402
from app.core.openviking_corpus import (  # noqa: E402
    build_corpus_specs,
    corpus_summary,
)
from app.core.openviking_live import (  # noqa: E402
    MAX_PAID_VLM_SOURCES_PER_RUN,
    LiveOpenVikingBackend,
    make_source_reader,
)

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def _host_of(url: str) -> str:
    return url.split("//")[-1].split("/")[0].split(":")[0]


def _health_ok(base_url: str, timeout: float) -> bool:
    import httpx

    try:
        r = httpx.get(base_url.rstrip("/") + "/health", timeout=timeout)
        if r.status_code != 200:
            return False
        return r.json().get("status") == "ok"
    except Exception:
        return False


def _server_version(base_url: str, timeout: float) -> Optional[str]:
    import httpx

    try:
        r = httpx.get(base_url.rstrip("/") + "/health", timeout=timeout)
        data = r.json()
        return data.get("version") or data.get("openviking_version")
    except Exception:
        return None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="D4a.1 live qualification runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:1933")
    parser.add_argument("--profile-skills-dir", default="~/.hermes-website/skills")
    parser.add_argument("--project-id", default="wb-design")
    parser.add_argument("--out", default="~/.website-builder/openviking/qualification.json")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--allow-remote", action="store_true")
    # COST CONTROL: the FREE path (vectors_only) is the DEFAULT. Selecting the
    # paid VLM path requires the explicit --allow-paid-vlm flag AND is bounded by
    # --paid-vlm-ceiling. This prevents an unapproved paid indexing run.
    parser.add_argument(
        "--processing-mode",
        choices=("semantic_and_vectors", "vectors_only"),
        default="vectors_only",
    )
    parser.add_argument(
        "--allow-paid-vlm",
        action="store_true",
        help="explicitly opt in to paid VLM (L0/L1) ingestion; required with "
             "--processing-mode semantic_and_vectors",
    )
    parser.add_argument("--paid-vlm-ceiling", type=int, default=MAX_PAID_VLM_SOURCES_PER_RUN)
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    if _host_of(base_url) not in LOOPBACK_HOSTS and not args.allow_remote:
        print(f"REFUSED: base URL {base_url} is not loopback (use --allow-remote to override)")
        return 2

    if args.processing_mode == "semantic_and_vectors" and not args.allow_paid_vlm:
        print("REFUSED: --processing-mode semantic_and_vectors makes PAID VLM calls; "
              "pass --allow-paid-vlm to opt in (or use vectors_only)")
        return 2

    if not _health_ok(base_url, args.timeout):
        print(f"REFUSED: no healthy OpenViking server at {base_url}/health")
        return 2

    api_key = os.environ.get("OPENVIKING_API_KEY") or None
    config = ovr.OpenVikingConfig(
        enabled=True, base_url=base_url, api_key=api_key, timeout_seconds=args.timeout
    )
    backend = LiveOpenVikingBackend(config)
    if args.processing_mode == "semantic_and_vectors":
        backend.enable_paid_vlm(ceiling=args.paid_vlm_ceiling)
    else:
        backend.processing_mode = "vectors_only"
    adapter = ovr.OpenVikingRetrievalAdapter(config, backend)

    skills_dir = Path(args.profile_skills_dir).expanduser()
    project_id = args.project_id
    evidence: Dict[str, Any] = {
        "base_url": base_url,
        "processing_mode": args.processing_mode,
        "server_version": _server_version(base_url, args.timeout),
        "pinned_version": lib.OPENVIKING_PINNED_VERSION,
        "project_id": project_id,
        "corpus": corpus_summary(skills_dir, project_id),
        "ingestion": {},
        "retrieval": {},
        "latency_ms": {},
    }

    # --- Ingestion -------------------------------------------------------
    specs = build_corpus_specs(skills_dir, project_id)
    reader = make_source_reader(skills_dir)
    t0 = time.monotonic()
    report = lib.ingest_sources(backend, specs, reader=reader, project_id=project_id)
    evidence["ingestion"] = {
        "elapsed_s": round(time.monotonic() - t0, 3),
        "statuses": dict(report.statuses),
        "total_bytes": report.total_bytes,
        "summary": report.summary(),
        "items": [
            {"source_id": i.source_id, "uri": i.uri, "status": i.status}
            for i in report.items
        ],
    }

    # Idempotency probe: re-ingest; unchanged sources must skip.
    report2 = lib.ingest_sources(backend, specs, reader=reader, project_id=project_id)
    evidence["ingestion"]["reingest_statuses"] = dict(report2.statuses)

    # --- Retrieval A-J ---------------------------------------------------
    def probe(name: str, query: str, *, scope=None, project=None, budget=None) -> Dict[str, Any]:
        pid = project or project_id
        start = time.monotonic()
        result = adapter.retrieve_context(query, pid, scope=scope, budget=budget)
        ms = (time.monotonic() - start) * 1000.0
        evidence["latency_ms"][name] = round(ms, 2)
        return {
            "status": result.status,
            "returned": result.returned_items,
            "uris": [i.uri for i in result.items],
            "revisions": [i.source_revision for i in result.items],
            "trusts": [i.trust for i in result.items],
            "categories": [i.category for i in result.items],
            "scores": [i.score for i in result.items],
            "levels": [i.level for i in result.items],
            "tokens": result.estimated_tokens,
            "bytes": result.total_bytes,
            "truncated": result.truncated,
            "warnings": list(result.warnings),
        }

    evidence["retrieval"]["A_design"] = probe(
        "A_design", "minimalist editorial landing page with botanical typography"
    )
    evidence["retrieval"]["B_component"] = probe(
        "B_component", "accessible responsive card component design"
    )
    evidence["retrieval"]["C_motion"] = probe(
        "C_motion", "subtle page transition with reduced motion accessibility"
    )
    evidence["retrieval"]["D_missing"] = probe(
        "D_missing", "quantum chromodynamics lattice gauge theory renormalization"
    )
    # D2: the same absent-topic query with an application-owned relevance floor.
    # It must return honestly empty (no fabrication) when nothing is relevant.
    evidence["retrieval"]["D_missing_floor"] = probe(
        "D_missing_floor",
        "quantum chromodynamics lattice gauge theory renormalization",
        budget=ovr.RetrievalBudget(min_score=0.65),
    )
    evidence["retrieval"]["E_cross_project"] = probe(
        "E_cross_project", "typography", project="a-different-project"
    )

    latencies = [v for v in evidence["latency_ms"].values()]
    if latencies:
        evidence["latency_ms"]["_median"] = round(statistics.median(latencies), 2)
        evidence["latency_ms"]["_max"] = round(max(latencies), 2)

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8")
    print(f"evidence written to {out}")
    print(json.dumps(evidence["ingestion"]["statuses"], sort_keys=True))
    for name in ("A_design", "B_component", "C_motion", "D_missing", "D_missing_floor", "E_cross_project"):
        r = evidence["retrieval"][name]
        print(f"{name}: status={r['status']} returned={r['returned']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
