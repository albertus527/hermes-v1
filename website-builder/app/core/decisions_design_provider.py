"""Decision Design: selects among REAL candidate ids supplied by the evidence pipeline.

OWNER CORRECTION B
------------------
"GPT-6 Luna Decisions must select or abstain among actual candidate IDs supplied
by the existing evidence pipeline. Never generate fictitious source_uri,
source_revision, or provenance."

This provider therefore does exactly one thing: it is handed a set of candidate
ids that the retrieval layer already produced, asks the model which id is best
grounded, and maps the answer back onto the *original* candidate object. It
never constructs a candidate, never invents provenance, and never fabricates an
id.

It implements the existing :class:`app.core.decision_provider.DecisionProvider`
protocol so the existing ``DecisionEngine`` / ``DecisionProviderRegistry``
machinery consumes it unchanged. ``classify_intake`` raises ``NOT_IMPLEMENTED``
because intake is a different role -- exactly the shape
``OpenVikingDecisionProvider`` already uses.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from app.core.decision_provider import (
    DecisionProviderError,
    DesignCandidate,
)
from app.core.decisions_client import (
    extract_choice,
    post_decision,
    resolved_model_id,
)
from app.core.design_reference import StructuralEvidence
from app.core.design_reference_safety import one_line
from app.core.design_scoring import (
    DEFAULT_DIMENSION_FLOORS,
    STATUS_ABSTAINED,
    STATUS_INSUFFICIENT_BRIEF,
    STATUS_INSUFFICIENT_EVIDENCE,
    STATUS_INSUFFICIENT_REQUIREMENTS,
    STATUS_STALE_EVIDENCE,
    SUITABILITY_CONTRACT,
    ScoringError,
    apply_gates,
    build_scoring_questions,
    build_scoring_state,
    evidence_is_current,
    evidence_sufficient,
    parse_pool_scores,
    assess_requirement_sufficiency,
)

logger = logging.getLogger(__name__)


def _asset_digest(asset: Any) -> str:
    """Read an asset's digest from either an object or a serialised mapping.

    A candidate's assets may be ``DesignAsset`` instances or the plain
    mappings produced by ``to_dict()``. Both are legitimate: one is the live
    object, the other is what came back from storage. Reading only one of them
    would silently drop every measurement on the other path -- which is exactly
    what happened before this helper existed.
    """
    if isinstance(asset, Mapping):
        return str(asset.get("digest", "") or "").strip()
    return str(getattr(asset, "digest", "") or "").strip()


def _verified_evidence_map(pool: Sequence[Any]) -> Dict[str, Any]:
    """Map candidate id -> measurement, for evidence that binds to it.

    The binding is checked at the point of USE, not trusted from wherever the
    evidence was produced. For each candidate the checks are:

      * the measurement names this candidate
      * the measurement's source revision is the candidate's revision
      * the measurement's screenshot digest is the digest of one of the
        candidate's OWN assets -- the image the model will actually see

    A measurement failing any check is dropped, so its dimension falls back to
    the screenshot-only tier. It is never repaired and never re-bound.

    This is deliberately a second check rather than a single one: the first
    happens when a reference is turned into a candidate, the second here, at
    the boundary where the request is actually built. Two independent points of
    verification means a mistake in either one alone cannot attach a
    measurement to the wrong image.
    """
    out: Dict[str, Any] = {}
    for candidate in pool:
        measurements = getattr(candidate, "measurements", ()) or ()
        if not measurements:
            continue
        candidate_id = str(getattr(candidate, "candidate_id", "") or "")
        source_revision = str(getattr(candidate, "source_revision", "") or "")
        digests = {
            _asset_digest(asset)
            for asset in (getattr(candidate, "assets", ()) or ())
        }
        digests.discard("")

        for evidence in measurements:
            if not isinstance(evidence, StructuralEvidence):
                continue
            if not evidence.collected:
                continue
            if evidence.candidate_id != candidate_id:
                continue
            if evidence.source_revision != source_revision:
                continue
            if evidence.screenshot_digest not in digests:
                continue
            out[candidate_id] = evidence
            break
    return out

#: Provider name registered with the DecisionProviderRegistry.
DECISIONS_DESIGN_PROVIDER_NAME = "decisions-design"

#: Question key used in the request/response envelope.
QUESTION_NAME = "choice"

#: Option offered when nothing is adequately grounded. The model must be able to
#: decline; forcing a pick is what produced the Stage 2B abstention failures.
ABSTAIN_OPTION = "ABSTAIN"
ABSTAIN_CRITERION = (
    "No candidate is adequately grounded by the brief and provenance."
)

#: Scope rule injected verbatim into the prompt. This is the D11 fix: the
#: website-only constraint must be VISIBLE to the model whenever it determines
#: the correct answer, never implicit.
SCOPE_RULE = (
    "Scope rule: this pipeline builds WEBSITE surfaces only. It cannot produce "
    "or validate logos, mascots, print collateral, or brand identity systems. "
    "A candidate that requires non-website output must not be chosen."
)


def build_design_state(
    *,
    brief: Mapping[str, Any],
    candidates: Sequence[DesignCandidate],
    include_scope_rule: bool = True,
    limit: int = 1400,
) -> str:
    """Render the ``state`` for a Design decision.

    Every candidate line carries the provenance fields the engine already
    holds, so the model chooses among real, cited options only.
    """
    lines = []
    # N8 B7: every interpolated value is untrusted reference text, so it passes
    # through one_line() before it can become prompt structure. A newline in a
    # label would otherwise fabricate what looks like application-authored lines.
    brief_what = one_line(brief.get("what") or "", 400)
    brief_why = one_line(brief.get("why") or "", 400)
    brief_name = one_line(brief.get("name") or "", 200)
    if brief_name or brief_what or brief_why:
        lines.append(f"Brief: {brief_name} {brief_what} {brief_why}".strip())
    intent = one_line(brief.get("design_intent") or "", 600)
    if intent:
        lines.append(f"Owner design intent (verbatim): {intent}")
    if include_scope_rule:
        lines.append(SCOPE_RULE)
    lines.append("")
    lines.append("Candidate references:")
    for candidate in candidates:
        lines.append(
            f"- {one_line(candidate.candidate_id, 80)}: "
            f"{one_line(candidate.label, 160)} | "
            f"provenance: {one_line(candidate.source_uri, 300)} @ "
            f"{one_line(candidate.source_revision, 64)} | "
            f"descriptor: {one_line(candidate.descriptor, 600)}"
        )
    lines.append("")
    lines.append(
        "Choose the single best-grounded candidate id, or ABSTAIN if none is "
        "adequately grounded by the brief and provenance."
    )
    return "\n".join(lines)[:limit]


def build_design_questions(candidates: Sequence[DesignCandidate]) -> dict:
    """Build the ``questions`` payload from the REAL candidate set.

    Only ids that actually exist are offered, plus ``ABSTAIN``. The model cannot
    name anything else because it is not an option.

    N8 B7: the criteria text embeds untrusted reference fields, so each one is
    sanitised. A newline inside a label would otherwise inject a fake criterion.
    """
    criteria: dict = {}
    for candidate in candidates:
        criteria[candidate.candidate_id] = one_line(
            f"{candidate.label}. Provenance: {candidate.source_uri} @ "
            f"{candidate.source_revision}. Descriptor: {candidate.descriptor}",
            900,
        )
    criteria[ABSTAIN_OPTION] = ABSTAIN_CRITERION
    return {
        QUESTION_NAME: {
            "type": "choice",
            "instructions": (
                "Which candidate is best grounded in the brief, subject to the "
                "stated scope rule? Answer ABSTAIN if none is adequately grounded."
            ),
            "criteria": criteria,
        }
    }


class DecisionsDesignProvider:
    """Proposes a grounded design selection using the Decisions API.

    Implements the existing ``DecisionProvider`` protocol's design half. The
    intake half raises ``NOT_IMPLEMENTED`` so the engine falls back
    deterministically for intake, exactly as ``OpenVikingDecisionProvider`` does.
    """

    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str,
        deadline_seconds: float = 20.0,
        include_scope_rule: bool = True,
        scoring_enabled: bool = False,
        min_quality: float = 0.70,
        min_fit: float = 0.70,
        min_suitability: float = 0.75,
        dimension_floors: Optional[Mapping[str, float]] = None,
        requirements: Sequence[str] = (),
    ) -> None:
        self.model = model
        #: Name-mangled and excluded from repr(), so logging the provider can
        #: never leak the credential.
        self.__api_key = api_key
        self.base_url = base_url
        self.deadline_seconds = deadline_seconds
        self.include_scope_rule = include_scope_rule
        #: N8 B7: multimodal scoring is OPT-IN. Off by default, so a provider
        #: built without explicit configuration keeps the grounding-only path
        #: and never claims a quality assessment.
        self.scoring_enabled = scoring_enabled
        #: Provisional quality standards. Configurable; NOT calibrated
        #: probabilities.
        self.min_quality = min_quality
        self.min_fit = min_fit
        self.min_suitability = min_suitability
        self.dimension_floors = dict(
            dimension_floors or DEFAULT_DIMENSION_FLOORS
        )
        #: N8 B7 calibration: the accepted requirement set from the native PRD.
        #: Empty by default -- a provider built without requirements abstains on
        #: Fit rather than inventing one.
        self.requirements: Tuple[str, ...] = tuple(requirements or ())
        #: Set by the last call so telemetry can distinguish abstain vs failure.
        self.last_error_code = ""
        self.last_degraded = False

    @property
    def _credential(self) -> str:
        return self.__api_key

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(model={self.model!r}, "
            f"base_url={self.base_url!r})"
        )

    @property
    def name(self) -> str:
        return DECISIONS_DESIGN_PROVIDER_NAME

    @property
    def enabled(self) -> bool:
        return bool(self.model) and bool(self._credential)

    # -- intake half of the protocol: deliberately unimplemented -------------

    def classify_intake(self, request_text: str, context: Mapping[str, Any]) -> Any:
        """Not this provider's job -- a design selector has no intake authority."""
        raise DecisionProviderError(
            "NOT_IMPLEMENTED", "decisions-design provides design selection only"
        )

    # -- N8 B7: multimodal scoring -----------------------------------------

    def _score_pool(
        self,
        *,
        real: Sequence[DesignCandidate],
        brief: Mapping[str, Any],
        requirement_version: str,
        limit: int,
        requirements: Sequence[str] = (),
    ) -> Sequence[DesignCandidate]:
        """Score the WHOLE eligible pool, or return nothing at all.

        All-or-nothing by construction: a partial response is a failed response,
        never a partially ranked success. An intentional abstention is reported
        as an abstention (``degraded=False``); a transport or parse failure is
        reported as degraded.
        """
        if not brief or not any(
            one_line(brief.get(k) or "") for k in ("what", "why", "name", "design_intent")
        ):
            self.last_error_code = STATUS_INSUFFICIENT_BRIEF
            return ()

        # Evidence gates. A candidate without rendered evidence, or whose assets
        # are bound to a different revision, is NOT scored -- never scored 0.0.
        eligible = [c for c in real if evidence_sufficient(c)]
        stale = [c for c in eligible if not evidence_is_current(c)]
        if stale:
            # A stale binding means we cannot trust which image we would send.
            self.last_error_code = STATUS_STALE_EVIDENCE
            self.last_degraded = True
            logger.info(
                "decisions design refused stale evidence",
                extra={"n8_role": "design", "n8_model": self.model,
                       "n8_error_class": STATUS_STALE_EVIDENCE,
                       "n8_stale": len(stale)},
            )
            return ()

        pool = eligible[:limit]
        if not pool:
            self.last_error_code = STATUS_INSUFFICIENT_EVIDENCE
            return ()

        # N8 B7 calibration: Fit is only assessed against requirements the native
        # pipeline actually produced. When the set cannot support a defensible
        # judgement this ABSTAINS -- it never scores Fit low. Missing
        # requirements are missing DATA, not a design-quality failure, and a
        # fabricated F would flow straight into the S gate as if it were real.
        requirements = tuple(requirements or ())
        # N9: sufficiency is an evidence-coverage judgement, not a count. Template
        # boilerplate is dropped here, so a set of compiler defaults cannot make
        # Fit look answerable when it is not.
        fit_usable, _fit_reason, requirements, _excluded = (
            assess_requirement_sufficiency(requirements)
        )

        state = build_scoring_state(
            brief=brief,
            candidates=pool,
            include_scope_rule=self.include_scope_rule,
            scope_rule=SCOPE_RULE,
            requirements=requirements,
        )
        # N8 B7 Option C: measured evidence travels with the request, but
        # ONLY evidence that binds to the candidate it is attached to. The
        # binding is re-verified HERE, at the point of use, rather than
        # trusted from wherever it was produced: a candidate's revision, its
        # screenshot digests and the measurement's own anchors must all
        # agree. Anything that does not agree is DROPPED, and that dimension
        # falls back to its screenshot-only tier.
        questions = build_scoring_questions(
            pool,
            requirements=requirements,
            evidence=_verified_evidence_map(pool),
        )

        result = post_decision(
            model=self.model,
            state=state,
            questions=questions,
            api_key=self._credential,
            base_url=self.base_url,
            deadline_seconds=self.deadline_seconds,
        )

        if not result.ok:
            self.last_error_code = result.error_code or "transport"
            self.last_degraded = True
            logger.info(
                "decisions design scoring failed",
                extra={"n8_role": "design", "n8_model": self.model,
                       "n8_error_class": result.error_code,
                       "n8_latency_ms": result.elapsed_ms},
            )
            return ()

        try:
            scores = parse_pool_scores(result.payload or {}, pool, expect_fit=fit_usable)
        except ScoringError as exc:
            self.last_error_code = exc.status
            # ``insufficient_requirements`` is an INTENTIONAL abstention: nothing
            # failed, the input was never sufficient. Marking it degraded would
            # make a data gap look like a provider outage.
            self.last_degraded = exc.status != STATUS_INSUFFICIENT_REQUIREMENTS
            logger.info(
                "decisions design returned unusable scores",
                extra={"n8_role": "design", "n8_model": self.model,
                       "n8_error_class": exc.status},
            )
            return ()

        passing, rejected = apply_gates(
            scores,
            min_quality=self.min_quality,
            min_fit=self.min_fit,
            min_suitability=self.min_suitability,
            floors=self.dimension_floors,
        )

        by_id = {c.candidate_id: c for c in pool}
        cost = None
        usage = result.payload.get("usage") if isinstance(result.payload, Mapping) else None
        if isinstance(usage, Mapping):
            try:
                cost = float(usage.get("cost"))
            except (TypeError, ValueError):
                cost = None

        logger.info(
            "decisions design scored pool",
            extra={
                "n8_role": "design",
                "n8_model": self.model,
                "n8_resolved_model": resolved_model_id(result.payload or {}),
                "n8_scored": len(scores),
                "n8_passed": len(passing),
                "n8_rejected": len(rejected),
                "n8_latency_ms": result.elapsed_ms,
                "n8_cost": cost,
            },
        )

        if not passing:
            # Every candidate was scored but none cleared the gates. This is an
            # INTENTIONAL abstention, not a failure.
            self.last_error_code = STATUS_ABSTAINED
            return ()

        out: list = []
        for score in passing:
            candidate = by_id[score.candidate_id]
            out.append(
                DesignCandidate(
                    candidate_id=candidate.candidate_id,
                    label=candidate.label,
                    descriptor=candidate.descriptor,
                    palette=candidate.palette,
                    source_uri=candidate.source_uri,
                    source_revision=candidate.source_revision,
                    # Retrieval score preserved verbatim.
                    score=candidate.score,
                    uncertainty=candidate.uncertainty,
                    suitability=score.suitability,
                    suitability_detail=score.detail(),
                    suitability_contract=SUITABILITY_CONTRACT,
                    evidence=candidate.evidence,
                    evidence_revision=candidate.evidence_revision,
                    assets=candidate.assets,
                    requirement_version=requirement_version,
                )
            )
        return tuple(out)

    # -- design half --------------------------------------------------------

    def propose_design_candidates(
        self,
        context: Mapping[str, Any],
        *,
        limit: int,
    ) -> Sequence[DesignCandidate]:
        """Ask the model to pick among the supplied candidates.

        ``context`` must carry ``candidates``: the real, already-provenanced
        options produced by the evidence pipeline. If it does not, this provider
        returns ``()`` -- it will never invent one.
        """
        self.last_error_code = ""
        self.last_degraded = False

        if not self.enabled or limit <= 0:
            return ()

        supplied = context.get("candidates") if isinstance(context, Mapping) else None
        if not supplied:
            # No real candidates => nothing to choose among. Abstain honestly.
            self.last_error_code = "no_candidates"
            return ()

        real: list = []
        for item in supplied:
            if isinstance(item, DesignCandidate):
                real.append(item)
            elif isinstance(item, Mapping):
                candidate = _coerce_candidate(item)
                if candidate is not None:
                    real.append(candidate)
        # Only candidates carrying provenance may reach the model at all.
        real = [c for c in real if c.has_provenance][:limit]
        if not real:
            self.last_error_code = "no_provenanced_candidates"
            return ()

        brief = context.get("brief") if isinstance(context, Mapping) else None
        brief = brief if isinstance(brief, Mapping) else {}

        # N8 B5: the requirement revision this fit judgement is tied to. Every
        # candidate in the pool carries the same value, which is what makes their
        # suitability scores comparable in the engine.
        requirement_version = ""
        #: N8 B7 calibration: requirements arrive per-call from the native PRD
        #: read in the build path. A per-call value overrides the constructor
        #: default so one provider instance cannot leak requirements between
        #: projects.
        call_requirements: Tuple[str, ...] = tuple(
            getattr(self, "requirements", ()) or ()
        )
        if isinstance(context, Mapping):
            requirement_version = str(
                context.get("requirement_version") or ""
            ).strip()
            supplied = context.get("requirements")
            if isinstance(supplied, Sequence) and not isinstance(
                supplied, (str, bytes)
            ):
                call_requirements = tuple(str(x) for x in supplied)

        # N8 B7: when scoring is enabled, take the multimodal scoring path
        # unconditionally. It decides for itself whether any candidate has
        # enough evidence; falling through to the grounding-only choice path
        # here would mean a scoring provider silently makes a NETWORK call it
        # was not asked to make, and would report "transport" instead of the
        # honest "insufficient_evidence".
        if self.scoring_enabled:
            return self._score_pool(
                real=real,
                brief=brief,
                requirement_version=requirement_version,
                limit=limit,
                requirements=call_requirements,
            )

        state = build_design_state(
            brief=brief,
            candidates=real,
            include_scope_rule=self.include_scope_rule,
        )
        questions = build_design_questions(real)

        result = post_decision(
            model=self.model,
            state=state,
            questions=questions,
            api_key=self._credential,
            base_url=self.base_url,
            deadline_seconds=self.deadline_seconds,
        )

        if not result.ok:
            self.last_error_code = result.error_code or "transport"
            self.last_degraded = True
            logger.info(
                "decisions design call failed",
                extra={
                    "n8_role": "design",
                    "n8_model": self.model,
                    "n8_error_class": result.error_code,
                    "n8_latency_ms": result.elapsed_ms,
                },
            )
            return ()

        payload = result.payload or {}
        choice, confidence, _cost = extract_choice(payload, QUESTION_NAME)

        if choice is None:
            self.last_error_code = "malformed"
            self.last_degraded = True
            return ()

        if choice == ABSTAIN_OPTION:
            # Intentional abstention. NOT a failure: the model declined.
            self.last_error_code = "abstained"
            return ()

        # Map the model's id back onto the ORIGINAL candidate object. Provenance
        # therefore comes from the evidence pipeline, never from the model.
        by_id = {c.candidate_id: c for c in real}
        selected = by_id.get(choice)
        if selected is None:
            # The model named an id that was not offered. Reject outright.
            self.last_error_code = "unknown_candidate"
            self.last_degraded = True
            logger.info(
                "decisions design returned an unoffered candidate id",
                extra={"n8_role": "design", "n8_model": self.model,
                       "n8_error_class": "unknown_candidate"},
            )
            return ()

        # N8 B6: the artificial score promotion is GONE.
        #
        # The old code rewrote the chosen candidate's score to 1.0 so the
        # engine's ``(-score, candidate_id)`` sort would pick it. That destroyed
        # the evidence pipeline's own ranking signal and encoded "the model named
        # this" as if it were a quality score.
        #
        # The model's preference is now expressed the only honest way available:
        # as an ORDER, with every candidate's retrieval score preserved exactly
        # as retrieved. Ranking by suitability is B7's job; until a suitability
        # score exists, the engine's own score-floor and provenance gates decide.
        ranked = [
            DesignCandidate(
                candidate_id=candidate.candidate_id,
                label=candidate.label,
                descriptor=candidate.descriptor,
                palette=candidate.palette,
                source_uri=candidate.source_uri,
                source_revision=candidate.source_revision,
                # Verbatim: never rewritten.
                score=candidate.score,
                uncertainty=candidate.uncertainty,
                suitability=candidate.suitability,
                suitability_detail=candidate.suitability_detail,
                suitability_contract=candidate.suitability_contract,
                evidence=candidate.evidence,
                evidence_revision=candidate.evidence_revision,
                assets=candidate.assets,
                requirement_version=requirement_version or candidate.requirement_version,
            )
            for candidate in real
        ]

        # Put the model's pick first so a stable sort preserves the preference
        # when scores tie. This changes ORDER only, never a score.
        ranked.sort(key=lambda c: c.candidate_id != selected.candidate_id)

        logger.info(
            "decisions design selection",
            extra={
                "n8_role": "design",
                "n8_model": self.model,
                "n8_resolved_model": resolved_model_id(payload),
                "n8_confidence": confidence,
                "n8_latency_ms": result.elapsed_ms,
            },
        )
        return tuple(ranked)


def _coerce_candidate(item: Mapping[str, Any]) -> Optional[DesignCandidate]:
    """Best-effort coercion of a mapping into a DesignCandidate.

    Returns ``None`` when required fields are missing, so a malformed input can
    never become a fabricated candidate.
    """
    try:
        candidate_id = str(item.get("candidate_id") or "").strip()
        label = str(item.get("label") or "").strip()
        descriptor = str(item.get("descriptor") or "").strip()
        if not candidate_id or not label:
            return None
        palette = item.get("palette") or ()
        if isinstance(palette, str):
            palette = (palette,)
        return DesignCandidate(
            candidate_id=candidate_id,
            label=label,
            descriptor=descriptor,
            palette=tuple(str(p) for p in palette),
            source_uri=str(item.get("source_uri") or "").strip(),
            source_revision=str(item.get("source_revision") or "").strip(),
            score=float(item.get("score") or 0.0),
            uncertainty=str(item.get("uncertainty") or ""),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "DECISIONS_DESIGN_PROVIDER_NAME",
    "QUESTION_NAME",
    "ABSTAIN_OPTION",
    "SCOPE_RULE",
    "DecisionsDesignProvider",
    "build_design_state",
    "build_design_questions",
]
