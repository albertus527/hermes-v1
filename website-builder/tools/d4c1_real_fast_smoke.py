#!/usr/bin/env python3
"""D4c.1 REAL FAST smoke -- exactly ONE real paid FAST model call per run.

Runs the REAL production intake seam (real ``IntakeProcessor`` + real
``HermesAdapter`` + real ``LayaContextPreparer`` + real D4a
``LiveOpenVikingBackend``) against the REAL configured FAST model
(``website_builder.models.FAST``), with NO recording stand-in.

Isolation guarantees (so no persisted user project state is touched):
  * a fresh temp ``HERMES_HOME`` (copy of the profile ``config.yaml`` + ``.env``)
    so the FAST session DB never touches the production profile;
  * a fresh temp state root, so no project state is read or written;
  * a single ``intake.process(...)`` call (which only READS state);
  * the brief is a mission constant, byte-identical between runs;
  * no conversation history is passed (fresh store => empty context).

Usage:
    python tools/d4c1_real_fast_smoke.py --run A   # baseline, injection OFF
    python tools/d4c1_real_fast_smoke.py --run B   # injection ON, multilingual ON

Writes a SECRET-FREE JSON evidence file to
``~/.website-builder/openviking/d4c1_real_fast_run_{A,B}.json``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.channels.telegram import NormalizedMessage  # noqa: E402
from app.core import laya_context as laya  # noqa: E402
from app.core.intake import IntakeProcessor  # noqa: E402
from app.core.openviking_retrieval import OpenVikingConfig, build_adapter  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.hermes.adapter import HermesAdapter  # noqa: E402

PROFILE = Path.home() / ".hermes-website"
SERVICE = "openviking-website"

# ---- FROZEN mission brief (byte-identical between runs) --------------------
BRIEF = (
    "Buatin website portofolio personal untuk seorang software engineer. "
    "Desainnya minimalis, editorial, modern, dengan tipografi yang kuat, "
    "warna netral, animasi halus, layout responsif, serta bagian hero, "
    "tentang saya, proyek, dan kontak. Website hanya frontend statis tanpa "
    "login, database, atau backend."
)

# Secret-looking patterns -> redact from any captured evidence.
_SECRET_PATTERNS = [
    re.compile(r"(sk-[A-Za-z0-9_\-]{12,})"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password|bearer)\s*[:=]\s*\S+"),
    re.compile(r"([A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{16,})"),
]


def _redact(text: str) -> str:
    out = text
    for pat in _SECRET_PATTERNS:
        out = pat.sub("<redacted>", out)
    return out


def _isolated_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="d4c1-home-"))
    shutil.copy2(PROFILE / "config.yaml", home / "config.yaml")
    env = PROFILE / ".env"
    if env.exists():
        shutil.copy2(env, home / ".env")
        (home / ".env").chmod(0o600)
    return home


class CapturingAdapter(HermesAdapter):
    """Real adapter that records the exact prompt + the live agent instance.

    It does NOT replace the model boundary: the REAL
    ``_run_fast_programmatic`` still runs (exactly one real paid call). We only
    wrap it to capture the prompt string and the AIAgent the runtime built, so
    token usage can be read from the real agent after the turn.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.captured: List[Dict[str, Any]] = []
        self.agents: List[Any] = []
        self.agent_kwargs: List[Dict[str, Any]] = []
        self.decisions: List[Dict[str, Any]] = []

    def fast_interpret(self, *args, **kwargs):  # type: ignore[override]
        decision = super().fast_interpret(*args, **kwargs)
        self.decisions.append(dict(decision))
        return decision

    def _run_fast_programmatic(self, prompt: str, **kwargs):  # type: ignore[override]
        import app.hermes.adapter as _mod

        real_aiagent = _mod.AIAgent
        recorded: List[Any] = []

        def _factory(*a, **k):
            self.agent_kwargs.append(dict(k))
            inst = real_aiagent(*a, **k)
            recorded.append(inst)
            return inst

        _mod.AIAgent = _factory  # type: ignore[assignment]
        t0 = time.monotonic()
        try:
            result = super()._run_fast_programmatic(prompt, **kwargs)
        finally:
            _mod.AIAgent = real_aiagent  # type: ignore[assignment]
        dt = (time.monotonic() - t0) * 1000.0
        agent = recorded[-1] if recorded else None
        if agent is not None:
            self.agents.append(agent)
        self.captured.append({
            "prompt": prompt,
            "prompt_chars": len(prompt),
            "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            "role": kwargs.get("role"),
            "enabled_toolsets_kwarg_absent": "enabled_toolsets" not in kwargs,
            "model_call_ms": round(dt, 2),
            "success": getattr(result, "success", None),
            "error": getattr(result, "error", None),
            "raw_response": getattr(result, "response", None),
        })
        return result


def _agent_usage(agent: Any) -> Dict[str, Any]:
    if agent is None:
        return {}
    return {
        "input_tokens": int(getattr(agent, "session_input_tokens", 0) or 0),
        "output_tokens": int(getattr(agent, "session_output_tokens", 0) or 0),
        "total_tokens": int(getattr(agent, "session_total_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(agent, "session_cache_read_tokens", 0) or 0),
        "reasoning_tokens": int(getattr(agent, "session_reasoning_tokens", 0) or 0),
        "estimated_cost_usd": str(getattr(agent, "session_estimated_cost_usd", "") or ""),
        "cost_status": getattr(agent, "session_cost_status", None),
        "cost_source": getattr(agent, "session_cost_source", None),
    }


def _extract_block(prompt: str) -> Optional[str]:
    start = prompt.find("=== LAYA CONTEXT")
    end = prompt.find("=== END LAYA CONTEXT ===")
    if start == -1 or end == -1:
        return None
    return prompt[start:end + len("=== END LAYA CONTEXT ===")]


def _aux_usage(home: Path, session_id: Optional[str]) -> Dict[str, Any]:
    """Read per-task model usage from the isolated session DB.

    Hermes records auxiliary calls (e.g. ``title_generation``) in
    ``session_model_usage``. This exposes whether a run triggered any
    non-FAST model call, so the call count is auditable.
    """
    db = home / "state.db"
    if not db.exists():
        return {"session_db_present": False, "tasks": [], "total_aux_calls": 0}
    try:
        import sqlite3

        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        cur = con.cursor()
        cur.execute(
            "SELECT task, model, api_call_count, input_tokens, output_tokens "
            "FROM session_model_usage ORDER BY task"
        )
        rows = cur.fetchall()
        con.close()
    except Exception as exc:
        return {"session_db_present": True, "error": type(exc).__name__}
    tasks = [
        {"task": (r[0] or ""), "model": r[1], "api_calls": r[2],
         "input_tokens": r[3], "output_tokens": r[4]}
        for r in rows
    ]
    aux = [t for t in tasks if t["task"]]
    return {
        "session_db_present": True,
        "tasks": tasks,
        "total_aux_calls": sum(t["api_calls"] for t in aux),
        "total_aux_input_tokens": sum(t["input_tokens"] for t in aux),
        "total_aux_output_tokens": sum(t["output_tokens"] for t in aux),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="D4c.1 real FAST smoke")
    ap.add_argument("--run", choices=["A", "B"], required=True)
    ap.add_argument("--base-url", default="http://127.0.0.1:1933")
    ap.add_argument("--project-id", default="wb-design")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    run = args.run
    injection = run == "B"
    multilingual = run == "B"
    # Preparation is ON for both runs (real service reachable); injection is the
    # only D4c variable. Run A: prep ON, injection OFF -> NO retrieval, no block.
    prep_enabled = True

    api_key = os.environ.get("OPENVIKING_API_KEY") or None
    home = _isolated_home()
    state_root = Path(tempfile.mkdtemp(prefix="d4c1-state-"))
    store = ProjectStateStore(state_root)

    ov_cfg = OpenVikingConfig(
        enabled=True, base_url=args.base_url, api_key=api_key, timeout_seconds=15.0,
    )
    ov_adapter = build_adapter(ov_cfg)
    laya_cfg = laya.LayaConfig(
        enabled=prep_enabled,
        library_project_id=args.project_id,
        min_score=0.62,
        multilingual_expansion=multilingual,
        fast_context_injection=injection,
    ).normalized()
    preparer = laya.LayaContextPreparer(laya_cfg, ov_adapter)

    adapter = CapturingAdapter(store=store, hermes_home=home, repo_root=ROOT)
    intake = IntakeProcessor(store, hermes_adapter=adapter, laya=preparer)

    msg = NormalizedMessage(event_id="d4c1", user_id="1", conversation_id="1", text=BRIEF)

    # Real OpenViking retrieval evidence (Run B only; Run A does no retrieval).
    retrieval: Dict[str, Any] = {}
    if injection:
        t0 = time.monotonic()
        prep = preparer.prepare_context(BRIEF, args.project_id)
        retrieval = {
            "status": prep.status,
            "quality": prep.quality,
            "items": len(prep.items),
            "queries": list(prep.queries),
            "retrieval_calls": prep.retrieval_calls,
            "estimated_chars": prep.estimated_chars,
            "estimated_tokens": prep.estimated_tokens,
            "truncated": prep.truncated,
            "degraded": prep.degraded,
            "latency_ms": round(prep.latency_ms, 2),
            "warnings": list(prep.warnings),
            "sources": [
                {
                    "source_id": i.source_id,
                    "category": i.category,
                    "trust": i.trust,
                    "revision": i.source_revision,
                    "relevance": round(i.relevance, 4),
                    "uri_scheme": (i.source_uri or "").split("://")[0],
                    "excerpt_chars": len(i.excerpt or ""),
                }
                for i in prep.items
            ],
            "probe_latency_ms": round((time.monotonic() - t0) * 1000.0, 2),
        }

    # ---- the ONE real paid FAST call ---------------------------------------
    t0 = time.monotonic()
    result = intake.process(msg, args.project_id)
    intake_ms = (time.monotonic() - t0) * 1000.0

    calls = adapter.captured
    last = calls[-1] if calls else {}
    prompt = last.get("prompt", "") or ""
    block = _extract_block(prompt)
    agent = adapter.agents[-1] if adapter.agents else None
    usage = _agent_usage(agent)
    akw = adapter.agent_kwargs[-1] if adapter.agent_kwargs else {}
    decision_parsed = adapter.decisions[-1] if adapter.decisions else {}

    parsed = {
        "readiness": result.readiness.value,
        "scope": result.scope.value,
        "brief": result.brief,
        "clarification_question": result.clarification_question,
        "clarification_reason": result.clarification_reason,
        "clarification_field": result.clarification_field,
    }
    # The raw FAST JSON decision (source field distinguishes real model vs fallback)
    raw = last.get("raw_response") or ""
    fast_json = None
    try:
        s = raw.find("{"); e = raw.rfind("}") + 1
        if s >= 0 and e > s:
            fast_json = json.loads(raw[s:e])
    except Exception:
        fast_json = None

    evidence = {
        "run": run,
        "config": {
            "laya_enabled": laya_cfg.enabled,
            "multilingual_expansion": laya_cfg.multilingual_expansion,
            "fast_context_injection": laya_cfg.fast_context_injection,
            "openviking_enabled": True,
            "preparer_effective_enabled": preparer.enabled,
            "injection_effective_enabled": preparer.fast_context_injection_enabled,
        },
        "brief_sha256": hashlib.sha256(BRIEF.encode("utf-8")).hexdigest(),
        "brief_chars": len(BRIEF),
        "retrieval": retrieval,
        "fast_call_count": len(calls),
        "fast_model_call": {
            "role": last.get("role"),
            "prompt_chars": last.get("prompt_chars"),
            "prompt_sha256": last.get("prompt_sha256"),
            "model_call_ms": last.get("model_call_ms"),
            "success": last.get("success"),
            "error": last.get("error"),
            "received_reference_block": bool(block),
            "reference_block_chars": len(block) if block else 0,
            "reference_block_sha256": (
                hashlib.sha256(block.encode("utf-8")).hexdigest() if block else None
            ),
            "reference_block_first_line": (
                block.splitlines()[0] if block else None
            ),
            "reference_block_last_line": (
                block.splitlines()[-1] if block else None
            ),
            "brief_present_verbatim_in_prompt": BRIEF in prompt,
            "brief_is_last_user_text": prompt.rstrip().endswith(BRIEF),
            "raw_response_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
            "raw_response_chars": len(raw),
        },
        "fast_decision_parsed": fast_json,
        "fast_decision_source": (fast_json or {}).get("source"),
        "fast_interpret_returned_decision": decision_parsed,
        "agent_construction": {
            "model": akw.get("model"),
            "provider": akw.get("provider"),
            "requested_provider": akw.get("requested_provider"),
            "enabled_toolsets": akw.get("enabled_toolsets"),
            "has_fallback_model": bool(akw.get("fallback_model")),
        },
        "intake_result": parsed,
        "intake_ms": round(intake_ms, 2),
        "usage": usage,
        "auxiliary_model_usage": _aux_usage(home, getattr(agent, "session_id", None)),
        "prompt_evidence_redacted": _redact(prompt),
    }

    out = Path(args.out) if args.out else (
        Path.home() / ".website-builder" / "openviking" / f"d4c1_real_fast_run_{run}.json"
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(evidence, indent=2, sort_keys=True, default=str),
                   encoding="utf-8")

    print(json.dumps({
        "run": run,
        "fast_call_count": len(calls),
        "received_reference_block": bool(block),
        "reference_block_chars": len(block) if block else 0,
        "brief_verbatim_in_prompt": BRIEF in prompt,
        "readiness": parsed["readiness"],
        "scope": parsed["scope"],
        "fast_decision_source": evidence["fast_decision_source"],
        "usage": usage,
        "intake_ms": evidence["intake_ms"],
        "retrieval_items": retrieval.get("items"),
        "retrieval_quality": retrieval.get("quality"),
        "evidence": str(out),
    }, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
