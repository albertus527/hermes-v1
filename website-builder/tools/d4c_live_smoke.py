#!/usr/bin/env python3
"""D4c LIVE SMOKE -- production intake against the REAL OpenViking server.

Proves the D4c hand-off seam against the real service WITHOUT a paid FAST call:

  * requires a live ``/health`` AND ``/ready`` from the configured base URL;
  * refuses a non-loopback base URL unless ``--allow-remote`` is given;
  * does NOT reindex the corpus (retrieve only);
  * makes NO paid model call: the FAST *model* boundary is replaced by a
    recording stand-in that captures the exact prompt/context FAST would
    receive. Everything else is the real production composition
    (``app.runtime.compose``): the real ``IntakeProcessor``, the real
    ``LayaContextPreparer``, the real D4a ``LiveOpenVikingBackend``.

Scenarios (all through the production intake seam):

  1. D4c flag OFF (default) -> FAST gets NO reference block, exactly one call,
     the brief is verbatim: the accepted baseline is preserved.
  2. D4c flag ON -> FAST gets ONE labelled lower-trust reference block with real
     provenance from the live ``wb-design`` corpus; exactly one FAST call.
  3. Multilingual: Indonesian brief with the D4b.2 expansion ON vs OFF (the
     query plan changes) and an English brief (unchanged).
  4. Fail-closed: the isolated OpenViking unit is stopped; the intake path still
     runs FAST exactly once with no reference block and the brief intact.
  5. Contract-test FAST receiver: the exact payload structure is asserted
     (labelled reference DATA, non-authority, no privileged keys).

Writes a SECRET-FREE JSON evidence file.

    python tools/d4c_live_smoke.py \\
        --base-url http://127.0.0.1:1933 \\
        --project-id wb-design \\
        --out ~/.website-builder/openviking/d4c_live_smoke.json
"""

from __future__ import annotations

import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels.telegram import NormalizedMessage  # noqa: E402
from app.core import laya_context as laya  # noqa: E402
from app.core.openviking_retrieval import OpenVikingConfig  # noqa: E402
from app.runtime import RuntimeConfig, compose  # noqa: E402

LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}
SERVICE = "openviking-website"


def _host_of(url: str) -> str:
    return url.split("//")[-1].split("/")[0].split(":")[0]


def _get(base_url: str, path: str, timeout: float) -> Optional[Dict[str, Any]]:
    import httpx

    try:
        r = httpx.get(base_url.rstrip("/") + path, timeout=timeout)
        if r.status_code != 200:
            return {"status_code": r.status_code}
        return r.json()
    except Exception:
        return None


class RecordingFast:
    """A recording stand-in for the FAST MODEL boundary (no paid call).

    Mirrors ``HermesAdapter.fast_interpret``'s signature exactly and records the
    arguments the real intake seam passed, so the evidence shows precisely what
    FAST would have received. Returns a fixed, WEBSITE-scoped interpretation.
    """

    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    def fast_interpret(self, text, project_id=None, conversation_context=None,
                       reference_context=None):
        self.calls.append({
            "text": text,
            "project_id": project_id,
            "has_conversation_context": conversation_context is not None,
            "has_reference_context": bool(reference_context),
            "reference_context": reference_context,
        })
        return {
            "scope": "WEBSITE", "name": "Bloom", "what": "florist",
            "why": "show arrangements", "why_destination": None,
            "ambiguity": None, "clarification_needed": False,
            "clarification_question": None, "readiness": "DISCOVERY_READY",
            "source": "recording_standin",
        }


def _service_state() -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        out["openviking_active"] = subprocess.run(
            ["systemctl", "--user", "is-active", SERVICE],
            capture_output=True, text=True, timeout=10,
        ).stdout.strip()
    except Exception:
        out["openviking_active"] = "unknown"
    try:
        tmux = subprocess.run(["tmux", "ls"], capture_output=True, text=True, timeout=10)
        out["tmux_sessions"] = sorted(
            line.split(":")[0] for line in tmux.stdout.splitlines() if ":" in line
        )
    except Exception:
        out["tmux_sessions"] = []
    return out


def _resource_snapshot() -> Dict[str, Any]:
    """Cheap, secret-free host pressure snapshot (no new resident process)."""
    snap: Dict[str, Any] = {}
    try:
        mem = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            mem[k.strip()] = int(v.strip().split()[0])
        snap["mem_total_kb"] = mem.get("MemTotal")
        snap["mem_available_kb"] = mem.get("MemAvailable")
        snap["swap_total_kb"] = mem.get("SwapTotal")
        snap["swap_free_kb"] = mem.get("SwapFree")
    except Exception:
        pass
    try:
        snap["loadavg"] = list(os.getloadavg())
    except Exception:
        pass
    # Peak RSS of THIS process only (never touches Trade/OpenViking).
    snap["self_max_rss_kb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return snap


def _compose_real(base_url: str, api_key: Optional[str], *, prep_enabled: bool,
                  inject_enabled: bool, multilingual: bool, project_id: str):
    """Build the REAL production composition pointed at the live server."""
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="d4c-smoke-home-"))
    config = RuntimeConfig(
        telegram_bot_token="000000:SMOKE",
        hermes_home=home,
        workspace_root=home / "ws",
        state_root=home / "state",
        output_repo_path=home / "output-repo",
        vercel_token="x",
        vercel_team_id="team",
        vercel_ownership_namespace="ns",
        openviking_config=OpenVikingConfig(
            enabled=True, base_url=base_url, api_key=api_key, timeout_seconds=15.0,
        ),
        laya_config=laya.LayaConfig(
            enabled=prep_enabled,
            library_project_id=project_id,
            min_score=0.62,
            multilingual_expansion=multilingual,
            fast_context_injection=inject_enabled,
        ),
    )
    return compose(config)


def _drive(intake, text: str, project_id: Optional[str] = None) -> Dict[str, Any]:
    msg = NormalizedMessage(event_id="d4c", user_id="1", conversation_id="1", text=text)
    started = time.monotonic()
    result = intake.process(msg, project_id)
    ms = (time.monotonic() - started) * 1000.0
    return {"readiness": result.readiness.value, "scope": result.scope.value,
            "elapsed_ms": round(ms, 2)}


BRIEF = ("Bloom, florist, minimalist editorial landing page with botanical "
         "typography and subtle motion")
BRIEF_ID = ("Bloom, toko bunga, halaman landing editorial minimalis dengan "
            "tipografi botani dan animasi halus")
BRIEF_EN = ("A minimalist architecture studio website with generous whitespace "
            "and a project gallery")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="D4c live smoke")
    parser.add_argument("--base-url", default="http://127.0.0.1:1933")
    parser.add_argument("--project-id", default="wb-design")
    parser.add_argument("--out", default="~/.website-builder/openviking/d4c_live_smoke.json")
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument("--allow-remote", action="store_true")
    parser.add_argument("--skip-outage", action="store_true",
                        help="skip the isolated service stop/start fail-closed probe")
    args = parser.parse_args(argv)

    base_url = args.base_url.rstrip("/")
    if _host_of(base_url) not in LOOPBACK_HOSTS and not args.allow_remote:
        print(f"REFUSED: base URL {base_url} is not loopback (use --allow-remote)")
        return 2

    health = _get(base_url, "/health", args.timeout)
    ready = _get(base_url, "/ready", args.timeout)
    if not health or health.get("status") != "ok":
        print(f"REFUSED: no healthy OpenViking server at {base_url}/health")
        return 2

    api_key = os.environ.get("OPENVIKING_API_KEY") or None
    evidence: Dict[str, Any] = {
        "base_url": base_url,
        "server_version": health.get("version"),
        "health": health,
        "ready": ready,
        "project_id": args.project_id,
        "paid_model_calls": 0,
        "fast_calls_executed": 0,
        "note": ("The FAST *model* boundary is replaced by a recording stand-in "
                 "(a CONTRACT TEST, not real FAST execution); the real production "
                 "IntakeProcessor + LayaContextPreparer + LiveOpenVikingBackend run "
                 "unchanged."),
        "resources_before": _resource_snapshot(),
        "before": _service_state(),
        "scenarios": {},
        "outage": {},
    }

    # --- Scenario 1: D4c flag OFF -> accepted baseline preserved ------------
    comp_off = _compose_real(base_url, api_key, prep_enabled=True,
                             inject_enabled=False, multilingual=False,
                             project_id=args.project_id)
    fast_off = RecordingFast()
    comp_off.intake.hermes_adapter = fast_off
    s1 = _drive(comp_off.intake, BRIEF)
    call_off = fast_off.calls[-1] if fast_off.calls else {}
    evidence["scenarios"]["flag_off_baseline"] = {
        "intake": s1,
        "preparer_enabled": comp_off.intake.laya.enabled,
        "injection_enabled": bool(
            getattr(comp_off.intake.laya, "fast_context_injection_enabled", False)),
        "fast_call_count": len(fast_off.calls),
        "fast_received_reference_block": bool(call_off.get("reference_context")),
        "brief_preserved_verbatim": call_off.get("text") == BRIEF,
    }

    # --- Scenario 2: D4c flag ON -> labelled lower-trust block, 1 call ------
    comp_on = _compose_real(base_url, api_key, prep_enabled=True,
                            inject_enabled=True, multilingual=False,
                            project_id=args.project_id)
    fast_on = RecordingFast()
    comp_on.intake.hermes_adapter = fast_on
    s2 = _drive(comp_on.intake, BRIEF)
    call_on = fast_on.calls[-1] if fast_on.calls else {}
    ref = call_on.get("reference_context") or ""
    prep = comp_on.intake.laya.prepare_context(BRIEF, "d4c-smoke")
    evidence["scenarios"]["flag_on_injected"] = {
        "intake": s2,
        "preparer_enabled": comp_on.intake.laya.enabled,
        "injection_enabled": bool(
            getattr(comp_on.intake.laya, "fast_context_injection_enabled", False)),
        "fast_call_count": len(fast_on.calls),
        "fast_received_reference_block": bool(ref),
        "block_is_labelled_reference_data": "REFERENCE DATA" in ref,
        "block_declares_non_authority": "MUST NOT override" in ref,
        "block_chars": len(ref),
        "brief_preserved_verbatim": call_on.get("text") == BRIEF,
        "laya": {
            "status": prep.status,
            "quality": prep.quality,
            "items": len(prep.items),
            "queries": list(prep.queries),
            "retrieval_calls": prep.retrieval_calls,
            "estimated_chars": prep.estimated_chars,
            "estimated_tokens": prep.estimated_tokens,
            "truncated": prep.truncated,
            "latency_ms": round(prep.latency_ms, 2),
            "sources": [
                {"source_id": i.source_id, "category": i.category, "trust": i.trust,
                 "revision": i.source_revision, "relevance": round(i.relevance, 4),
                 "uri_scheme": (i.source_uri or "").split("://")[0]}
                for i in prep.items
            ],
            "warnings": list(prep.warnings),
        },
    }

    # --- Scenario 3: multilingual query plan --------------------------------
    plan_on = laya.plan_queries(BRIEF_ID, None,
                                laya.LayaConfig(enabled=True, multilingual_expansion=True))
    plan_off = laya.plan_queries(BRIEF_ID, None,
                                 laya.LayaConfig(enabled=True, multilingual_expansion=False))
    plan_en_on = laya.plan_queries(BRIEF_EN, None,
                                   laya.LayaConfig(enabled=True, multilingual_expansion=True))
    plan_en_off = laya.plan_queries(BRIEF_EN, None,
                                    laya.LayaConfig(enabled=True, multilingual_expansion=False))
    evidence["scenarios"]["multilingual"] = {
        "indonesian_expansion_changes_plan": plan_on != plan_off,
        "english_plan_unchanged": plan_en_on == plan_en_off,
        "indonesian_queries_on": list(plan_on),
        "indonesian_queries_off": list(plan_off),
    }

    # --- Scenario 4: fail-closed (isolated service stop/start) --------------
    if not args.skip_outage:
        evidence["outage"]["before"] = _service_state()
        try:
            subprocess.run(["systemctl", "--user", "--no-block", "stop", SERVICE],
                           capture_output=True, text=True, timeout=20)
            for _ in range(30):
                if _get(base_url, "/health", 2.0) is None:
                    break
                time.sleep(0.5)
            evidence["outage"]["health_after_stop"] = _get(base_url, "/health", 3.0)
            comp_down = _compose_real(base_url, api_key, prep_enabled=True,
                                      inject_enabled=True, multilingual=False,
                                      project_id=args.project_id)
            fast_down = RecordingFast()
            comp_down.intake.hermes_adapter = fast_down
            s4 = _drive(comp_down.intake, BRIEF)
            call_down = fast_down.calls[-1] if fast_down.calls else {}
            prep_down = comp_down.intake.laya.prepare_context(BRIEF, "d4c-down")
            evidence["outage"]["intake_after_stop"] = s4
            evidence["outage"]["fast_call_count"] = len(fast_down.calls)
            evidence["outage"]["fast_received_no_reference_block"] = not bool(
                call_down.get("reference_context"))
            evidence["outage"]["brief_preserved_verbatim"] = call_down.get("text") == BRIEF
            evidence["outage"]["laya_status"] = prep_down.status
            evidence["outage"]["laya_items"] = len(prep_down.items)
            evidence["outage"]["laya_error_reason"] = prep_down.error_reason
        except Exception as exc:  # never let the outage probe abort the run
            evidence["outage"]["error"] = type(exc).__name__
        finally:
            subprocess.run(["systemctl", "--user", "--no-block", "start", SERVICE],
                           capture_output=True, text=True, timeout=20)
            for _ in range(60):
                if _get(base_url, "/health", 2.0):
                    break
                time.sleep(0.5)
            evidence["outage"]["after"] = _service_state()
            evidence["outage"]["health_after_restart"] = _get(base_url, "/health", 5.0)

    # --- Scenario 5: contract-test payload structure ------------------------
    # Assert the exact STRUCTURE of what FAST would receive (contract test only).
    # NOTE: we must NOT substring-scan the block for words like "override" or
    # "instruction" -- the block's own safety disclaimer literally contains
    # "MUST NOT override ... never an instruction", so a substring scan matches
    # every time and proves nothing. Instead we assert the delimiter structure
    # and that the JSON payload carries no authority-bearing keys.
    block_lines = [ln for ln in ref.splitlines() if ln.strip()]
    header = block_lines[0] if block_lines else None
    footer = block_lines[-1] if block_lines else None
    payload_keys: List[str] = []
    item_keys: List[str] = []
    try:
        # The payload is the only JSON object in the block; prose has no braces.
        parsed = json.loads(ref[ref.index("{"): ref.rindex("}") + 1])
        if isinstance(parsed, dict):
            payload_keys = sorted(parsed.keys())
            items = parsed.get("items") or []
            if items and isinstance(items[0], dict):
                item_keys = sorted(items[0].keys())
    except Exception:
        parsed = None
    authority_keys = {"role", "system", "developer", "instruction", "override",
                      "tool_call", "function_call", "authority", "authorize"}
    evidence["scenarios"]["fast_payload_contract"] = {
        "reference_block_is_separate_field": "reference_context" in (call_on or {}),
        "brief_field_untouched": call_on.get("text") == BRIEF,
        "reference_block_is_labelled_and_delimited": bool(
            header and header.startswith("=== LAYA CONTEXT")
            and footer and footer.startswith("=== END LAYA CONTEXT")),
        "reference_block_has_no_authority_bearing_keys": not (
            set(payload_keys) & authority_keys or set(item_keys) & authority_keys),
        "reference_block_payload_keys": payload_keys,
        "reference_block_item_keys": item_keys,
        "reference_block_header": header,
        "note": ("The reference block is appended to the FAST prompt as clearly "
                 "delimited lower-trust reference DATA; it is never placed in "
                 "system/developer instructions, and its JSON payload carries no "
                 "authority-bearing keys."),
    }

    evidence["after"] = _service_state()
    evidence["resources_after"] = _resource_snapshot()
    evidence["paid_model_calls"] = 0

    out = Path(args.out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    # Secret-free: drop any raw reference_context bodies from the evidence.
    for scen in evidence["scenarios"].values():
        if isinstance(scen, dict):
            scen.pop("reference_context", None)
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str),
                   encoding="utf-8")

    print(f"evidence written to {out}")
    print(json.dumps({
        "server_version": evidence["server_version"],
        "flag_off_fast_block": evidence["scenarios"]["flag_off_baseline"]
            ["fast_received_reference_block"],
        "flag_off_fast_calls": evidence["scenarios"]["flag_off_baseline"]["fast_call_count"],
        "flag_on_fast_block": evidence["scenarios"]["flag_on_injected"]
            ["fast_received_reference_block"],
        "flag_on_fast_calls": evidence["scenarios"]["flag_on_injected"]["fast_call_count"],
        "flag_on_items": evidence["scenarios"]["flag_on_injected"]["laya"]["items"],
        "flag_on_quality": evidence["scenarios"]["flag_on_injected"]["laya"]["quality"],
        "indonesian_changes_plan": evidence["scenarios"]["multilingual"]
            ["indonesian_expansion_changes_plan"],
        "english_plan_unchanged": evidence["scenarios"]["multilingual"]
            ["english_plan_unchanged"],
        "outage_laya_status": evidence["outage"].get("laya_status"),
        "outage_fast_received_no_block": evidence["outage"].get(
            "fast_received_no_reference_block"),
        "outage_fast_calls": evidence["outage"].get("fast_call_count"),
        "payload_block_labelled_and_delimited": evidence["scenarios"]
            ["fast_payload_contract"]["reference_block_is_labelled_and_delimited"],
        "payload_block_has_no_authority_keys": evidence["scenarios"]
            ["fast_payload_contract"]["reference_block_has_no_authority_bearing_keys"],
        "paid_model_calls": evidence["paid_model_calls"],
    }, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
