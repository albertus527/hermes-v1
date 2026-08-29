"""R2.7 §11.4 — the news-classifier calibration framework.

BLOCKED externally: the ≥200 manually labeled historical headlines are a
Phase-2 exit-gate artifact that does NOT exist in this repository. This
module provides the framework ONLY — the labeled-set schema/loader, the
deterministic scoring of a labeled set against classifications, and the
report shape. It fabricates nothing: an empty or undersized labeled set
yields a report whose ``meets_minimum`` is False and whose status is
NOT EVALUABLE; it NEVER claims calibration passed.

The pre-registered label set is (category, direction, severity, ma_role)
per §11.4; ``keyword_override`` is deterministic code behavior and is not
a human label (§11.4). ``ma_role`` must obey the §11.1 conditional
validity rule inside the labeled set too.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from trading_core.news_effects import Classification

from backtest.news.cache import (
    MalformedClassificationError,
    validate_classification_payload,
)

MIN_LABELED_HEADLINES = 200          # §11.4 exit gate
LABEL_FIELDS = ("category", "direction", "severity", "ma_role")


class CalibrationDatasetError(Exception):
    """The labeled dataset violates the §11.4 schema (fail-closed)."""


@dataclass(frozen=True)
class LabeledHeadline:
    """One manually labeled calibration headline (§11.4)."""
    ticker: str
    headline_text: str
    published_at: str          # ISO-8601, timezone-aware
    source: str
    label: dict                # {category, direction, severity, ma_role}


def load_labeled_headlines(path) -> list[LabeledHeadline]:
    """Load a calibration labeled set from a JSON file with the shape::

        {"headlines": [
            {"ticker": "AAPL", "headline_text": "...",
             "published_at": "2024-01-05T09:30:00-05:00",
             "source": "finnhub",
             "label": {"category": "EARNINGS", "direction": "BULLISH",
                        "severity": "MEDIUM", "ma_role": "NEITHER"}},
            ...
        ]}

    Fail-closed validation: every label field must be a valid enum value
    and satisfy the §11.1 ma_role conditional rule; a malformed entry
    raises :class:`CalibrationDatasetError` rather than being skipped.
    """
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    if not isinstance(doc, dict) or not isinstance(doc.get("headlines"), list):
        raise CalibrationDatasetError(
            "labeled set must be a JSON object with a 'headlines' array")
    out: list[LabeledHeadline] = []
    for i, item in enumerate(doc["headlines"]):
        try:
            label = item["label"]
            for f in ("ticker", "headline_text", "published_at", "source"):
                if not isinstance(item.get(f), str) or not item[f]:
                    raise CalibrationDatasetError(f"{f} must be a non-empty string")
            if not isinstance(label, dict) or \
                    set(label) != set(LABEL_FIELDS):
                raise CalibrationDatasetError(
                    f"label must have exactly {LABEL_FIELDS} fields")
            # Reuse the strict §11.1 validation for enum + conditional
            # validity by round-tripping through the payload validator.
            validate_classification_payload({
                "ticker": item["ticker"],
                "category": label["category"],
                "direction": label["direction"],
                "severity": label["severity"],
                "ma_role": label["ma_role"],
                "confidence": 1.0,
                "published_at": item["published_at"],
                "headline_hash": "x" * 64,
                "source": item["source"],
                "keyword_override": False,
                "schema_version": "news_schema_v3",
                "model_version": "calibration-label",
            })
        except (KeyError, MalformedClassificationError) as exc:
            raise CalibrationDatasetError(
                f"labeled headline #{i} is invalid: {exc}") from exc
        out.append(LabeledHeadline(
            ticker=item["ticker"], headline_text=item["headline_text"],
            published_at=item["published_at"], source=item["source"],
            label=dict(label)))
    return out


@dataclass
class CalibrationReport:
    """§11.4 accuracy + confidence-calibration report shape."""
    labeled_count: int = 0
    classified_count: int = 0
    exact_label_match: int = 0
    per_field_correct: dict = field(default_factory=dict)
    accuracy: float | None = None
    meets_minimum: bool = False                # labeled_count >= 200
    status: str = "NOT EVALUABLE"              # never "PASS" from here
    mean_confidence_when_correct: float | None = None
    mean_confidence_when_wrong: float | None = None
    calibration_note: str = (
        "Threshold 0.85 remains ASSUMPTION pending calibration (§11.4).")

    def to_json(self) -> str:
        return json.dumps(self.__dict__, sort_keys=True, indent=2)


def evaluate_calibration(
    labeled: list[LabeledHeadline],
    classifications: dict[str, Classification],
) -> CalibrationReport:
    """Score classifications against labels.

    ``classifications`` maps headline TEXT (normalized comparison via FP-4
    headline_hash is used internally) to the classifier's output. Entries
    missing a classification count as mismatches (fail-closed), and a
    labeled set smaller than §11.4's minimum can never produce a
    ``meets_minimum=True`` report regardless of accuracy.
    """
    import datetime as _dt
    report = CalibrationReport(labeled_count=len(labeled))
    by_hash: dict[str, Classification] = {}
    for cls in classifications.values():
        by_hash[cls.headline_hash] = cls
    from trading_core.news_effects import headline_hash as hh
    field_correct = {f: 0 for f in LABEL_FIELDS}
    conf_correct: list[float] = []
    conf_wrong: list[float] = []
    for lh in labeled:
        report.classified_count += 1
        cls = by_hash.get(hh(lh.headline_text))
        if cls is None:
            continue  # missing classification — counts as full mismatch
        all_match = True
        for f in LABEL_FIELDS:
            if getattr(cls, f) == lh.label[f]:
                field_correct[f] += 1
            else:
                all_match = False
        if all_match:
            report.exact_label_match += 1
            conf_correct.append(cls.confidence)
        else:
            conf_wrong.append(cls.confidence)
    report.per_field_correct = field_correct
    if labeled:
        report.accuracy = report.exact_label_match / len(labeled)
    if conf_correct:
        report.mean_confidence_when_correct = sum(conf_correct) / len(conf_correct)
    if conf_wrong:
        report.mean_confidence_when_wrong = sum(conf_wrong) / len(conf_wrong)
    report.meets_minimum = len(labeled) >= MIN_LABELED_HEADLINES
    if not labeled:
        report.status = "NOT EVALUABLE — empty labeled set"
    elif not report.meets_minimum:
        report.status = (
            f"NOT EVALUABLE — labeled set below §11.4 minimum "
            f"({len(labeled)} < {MIN_LABELED_HEADLINES})")
    else:
        # Even with a sufficient set, "PASS" is a human decision after
        # reviewing the report; the framework never claims it.
        report.status = "EVALUATED — awaiting human review"
    return report
