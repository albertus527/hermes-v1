"""Post-lock reveal of prior consensus and the pinned model prediction.

Reveal is opt-in and strictly gated: it is only ever constructed for a sample
whose reviewer row is LOCKED. Nothing here is reachable before lock.

Two independent, separately opt-in reveals:

* ``consensus`` — the prior ``LLM_CONSENSUS_SPOT_CHECKED`` label and notes
  from the source CSV (never presented as human-adjudicated ground truth).
* ``model`` — the pinned ``openrouter/openai/gpt-6-luna`` prediction read from
  the production cache in SQLite read-only URI mode. The production store is
  never written.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from backtest.calibration_reviewer.datasets import LoadedDataset

MODEL_PIN = "openrouter/openai/gpt-6-luna"
SCHEMA_VERSION = "news_schema_v3"
CONSENSUS_PROVENANCE = "LLM_CONSENSUS_SPOT_CHECKED"


class ModelPredictionUnavailable(Exception):
    """The pinned model prediction could not be read (fail-soft, disclosed)."""


def consensus_reveal(loaded: LoadedDataset, sample_id: str) -> dict:
    """Prior consensus label + notes for a locked sample."""
    secret = loaded.secrets.get(sample_id, {})
    raw_label = secret.get("human_label") or ""
    label = None
    if raw_label:
        try:
            label = json.loads(raw_label)
        except json.JSONDecodeError:
            label = {"raw": raw_label}
    return {
        "kind": "consensus",
        "provenance": CONSENSUS_PROVENANCE,
        "disclaimer": (
            "Prior labels were established by multi-LLM consensus with limited "
            "human spot-checking. They are NOT independently human-adjudicated "
            "ground truth."),
        "label": label,
        "notes": secret.get("human_notes") or "",
        # Selection cues are disclosed post-lock for transparency only.
        "challenge_reason": secret.get("challenge_reason") or None,
        "candidate_dimension": secret.get("candidate_dimension") or None,
        "direct_event_evidence": secret.get("direct_event_evidence") or None,
    }


def model_reveal(db_path: str | Path, *, headline_hash: str, ticker: str) -> dict:
    """Pinned model prediction for a locked sample, read-only.

    Returns a dict with ``available=False`` and a reason when the cache has no
    row for ``(headline_hash, ticker)`` under the pin — never fabricates one.
    """
    path = Path(db_path)
    if not path.exists():
        return {"kind": "model", "available": False,
                "reason": "production cache not present"}
    uri = f"file:{path}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.OperationalError as exc:
        raise ModelPredictionUnavailable(str(exc)) from exc
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        row = conn.execute(
            "SELECT json_payload FROM news_classifications WHERE "
            "headline_hash=? AND ticker=? AND model_version=? AND "
            "schema_version=?",
            (headline_hash, ticker, MODEL_PIN, SCHEMA_VERSION)).fetchone()
    finally:
        conn.close()
    if row is None:
        return {"kind": "model", "available": False,
                "model_pin": MODEL_PIN,
                "reason": "no cached prediction for this headline under the pin"}
    payload = json.loads(row["json_payload"])
    return {
        "kind": "model",
        "available": True,
        "model_pin": MODEL_PIN,
        "schema_version": SCHEMA_VERSION,
        "label": {
            "category": payload.get("category"),
            "direction": payload.get("direction"),
            "severity": payload.get("severity"),
            "ma_role": payload.get("ma_role"),
        },
        "confidence": payload.get("confidence"),
        "keyword_override": payload.get("keyword_override"),
        "disclaimer": (
            "Pinned model prediction (GPT-6 Luna). Shown for comparison only; "
            "it does not overwrite the human decision."),
    }
