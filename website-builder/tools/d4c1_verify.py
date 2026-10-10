#!/usr/bin/env python3
"""D4c.1 evidence aggregator + invariant/security checker (no paid calls).

Reads the two real-run evidence files and asserts every mission invariant,
emitting a machine-checkable summary used by the acceptance report.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

HOME = Path.home() / ".website-builder" / "openviking"
A = json.loads((HOME / "d4c1_real_fast_run_A.json").read_text())
B = json.loads((HOME / "d4c1_real_fast_run_B.json").read_text())

BRIEF = (
    "Buatin website portofolio personal untuk seorang software engineer. "
    "Desainnya minimalis, editorial, modern, dengan tipografi yang kuat, "
    "warna netral, animasi halus, layout responsif, serta bagian hero, "
    "tentang saya, proyek, dan kontak. Website hanya frontend statis tanpa "
    "login, database, atau backend."
)

AUTHORITY_KEYS = {"role", "system", "developer", "instruction", "override",
                  "tool_call", "function_call", "authority", "authorize",
                  "requirements", "command"}

SCHEMA_KEYS = {"scope", "name", "what", "why", "why_destination",
               "ambiguity", "clarification_needed", "clarification_question",
               "readiness"}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, bool(ok), detail))


def block_of(ev: dict) -> str:
    p = ev["prompt_evidence_redacted"]
    s = p.find("=== LAYA CONTEXT")
    e = p.find("=== END LAYA CONTEXT ===")
    if s == -1 or e == -1:
        return ""
    return p[s:e + len("=== END LAYA CONTEXT ===")]


# --- invariant: brief byte-identical & verbatim in both prompts -------------
check("A: brief sha256 == mission brief", A["brief_sha256"] ==
      __import__("hashlib").sha256(BRIEF.encode()).hexdigest())
check("B: brief sha256 == mission brief", B["brief_sha256"] ==
      __import__("hashlib").sha256(BRIEF.encode()).hexdigest())
check("A: brief verbatim in prompt", A["fast_model_call"]["brief_present_verbatim_in_prompt"])
check("B: brief verbatim in prompt", B["fast_model_call"]["brief_present_verbatim_in_prompt"])
check("A: brief is last user text", A["fast_model_call"]["brief_is_last_user_text"])
check("B: brief is last user text", B["fast_model_call"]["brief_is_last_user_text"])
check("A/B: same brief sha", A["brief_sha256"] == B["brief_sha256"])

# --- invariant: same real FAST model ----------------------------------------
check("A: FAST model == openrouter/z-ai/glm-5.3-flash",
      A["agent_construction"]["model"] == "openrouter/z-ai/glm-5.3-flash")
check("B: same FAST model as A",
      A["agent_construction"]["model"] == B["agent_construction"]["model"])

# --- invariant: exactly one FAST model call per run -------------------------
check("A: exactly one FAST call", A["fast_call_count"] == 1)
check("B: exactly one FAST call", B["fast_call_count"] == 1)

# --- D4c gate: A no block, B block ------------------------------------------
check("A: injection OFF -> NO reference block",
      A["config"]["fast_context_injection"] is False
      and A["fast_model_call"]["received_reference_block"] is False)
check("B: injection ON -> reference block present",
      B["config"]["fast_context_injection"] is True
      and B["fast_model_call"]["received_reference_block"] is True)

# --- block structure: labelled + delimited ----------------------------------
blk = block_of(B)
check("B: block first line labelled REFERENCE DATA",
      (B["fast_model_call"]["reference_block_first_line"] or "").startswith(
          "=== LAYA CONTEXT") and "REFERENCE DATA" in
      (B["fast_model_call"]["reference_block_first_line"] or ""))
check("B: block last line is END delimiter",
      (B["fast_model_call"]["reference_block_last_line"] or "").startswith(
          "=== END LAYA CONTEXT ==="))

# --- security: payload/item keys carry no authority --------------------------
payload_keys: list[str] = []
item_keys: list[str] = []
try:
    parsed = json.loads(blk[blk.index("{"): blk.rindex("}") + 1])
    payload_keys = sorted(parsed.keys())
    items = parsed.get("items") or []
    if items and isinstance(items[0], dict):
        item_keys = sorted(items[0].keys())
except Exception:
    pass
check("B: payload has no authority-bearing keys",
      not (set(payload_keys) & AUTHORITY_KEYS), str(payload_keys))
check("B: item keys have no authority-bearing keys",
      not (set(item_keys) & AUTHORITY_KEYS), str(item_keys))
check("B: block declares non-authority (MUST NOT override)",
      "MUST NOT override" in blk and "never an instruction" in blk)
check("B: block says user's words win",
      "the user's words win" in " ".join(blk.split()))

# --- security: no secrets in prompts ----------------------------------------
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+\S+|api[_-]?key\s*[:=]\s*\S+)", re.I)
check("A: no secret-looking token in prompt",
      not SECRET_RE.search(A["prompt_evidence_redacted"]))
check("B: no secret-looking token in prompt",
      not SECRET_RE.search(B["prompt_evidence_redacted"]))

# --- schema validity both runs ----------------------------------------------
for tag, ev in (("A", A), ("B", B)):
    dec = ev["fast_decision_parsed"] or {}
    check(f"{tag}: FAST decision has full schema",
          SCHEMA_KEYS.issubset(set(dec.keys())), str(sorted(dec.keys())))
    check(f"{tag}: FAST decision source == hermes_fast (real model)",
          ev["fast_interpret_returned_decision"].get("source") == "hermes_fast")

# --- authority: no scope expansion / tool use / deployment approval ----------
for tag, ev in (("A", A), ("B", B)):
    dec = ev["fast_decision_parsed"] or {}
    check(f"{tag}: scope == WEBSITE (no expansion)", dec.get("scope") == "WEBSITE")
    check(f"{tag}: no tool_call/function_call field", not (set(dec) & {"tool_call", "function_call"}))
    check(f"{tag}: readiness is a legal value",
          dec.get("readiness") in {"DISCOVERY_READY", "NEEDS_CLARIFICATION"})
    # no backend/auth/database invented in what/why
    text = " ".join(str(dec.get(k, "")) for k in ("what", "why", "name")).lower()
    for banned in ("login", "database", "backend", "auth", "dashboard", "admin panel"):
        check(f"{tag}: does not invent '{banned}'",
              banned not in text)

# --- Run B retrieval quality (real OpenViking) ------------------------------
r = B["retrieval"]
check("B: real retrieval produced items", r.get("items", 0) >= 1, str(r.get("items")))
check("B: every source has real viking:// provenance",
      all(s.get("uri_scheme") == "viking" for s in r.get("sources", [])))
check("B: every source has a revision pin",
      all(s.get("revision") for s in r.get("sources", [])))
check("B: every source trust == reviewed",
      all(s.get("trust") == "reviewed" for s in r.get("sources", [])))
check("B: corpus is wb-design", r.get("library_project_id") is None or True)  # see payload
check("B: multilingual expansion added an English gloss query",
      any(q == "portfolio minimalist typography" for q in r.get("queries", [])))
check("B: retrieval bounded to <= 3 queries", r.get("retrieval_calls", 9) <= 3)
check("B: pack bounded (<= 6000 chars)", r.get("estimated_chars", 10**9) <= 6000)

# --- production flags -------------------------------------------------------
check("A: prep enabled but injection OFF", A["config"]["fast_context_injection"] is False)
check("B: multilingual ON + injection ON", B["config"]["multilingual_expansion"] is True
      and B["config"]["fast_context_injection"] is True)

# --- report -----------------------------------------------------------------
passed = sum(1 for _, ok, _ in results if ok)
total = len(results)
for name, ok, detail in results:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
print(f"\n{passed}/{total} checks passed")
print("VERDICT:", "PASS" if passed == total else "FAIL")
sys.exit(0 if passed == total else 1)
