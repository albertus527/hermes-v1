"""Decision Engine modes and configuration (N6).

The approved N6 design gives the Decision Engine **two independent modes**, one
for each job it does:

* **Intake mode** — how the Decision Engine's intake classification is used.
* **Design mode** — whether the Decision Engine runs *before* the initial
  FRONTEND build to select bounded, provenance-validated design candidates.

Each mode is one of a closed set:

* ``disabled`` — the engine is not consulted at all. This is the default, and it
  is what makes the pre-N6 production path byte-identical.
* ``shadow`` — the engine is consulted and its answer is *recorded for
  observability only*. It never changes the result, the prompt, the published
  bytes, or an approval decision.
* ``active`` — the engine's answer is used. For intake, "used" means **only**
  new/revision routing and reference detection; FAST remains authoritative for
  scope, facts, requirements, and readiness. For design, "used" means the
  selected candidates are compiled into pre-build Design Guidance.

Two properties are load-bearing and are why this is a separate module:

**A malformed or missing configuration never silently enables the engine.** Any
unrecognised mode string, wrong type, or absent block yields ``disabled`` — the
same posture as :class:`app.core.requirement_creator.RequirementCreatorConfig`.

**The mode set is closed.** ``DecisionEngineMode`` is a ``str`` enum; a caller
can compare against it and a test can enumerate every value, so "which modes
exist" is a checkable fact rather than a comment.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional

#: Hard cap on candidates the Decision Engine Design path may ever consider,
#: independent of configuration. Mirrors ``decision_engine.MAX_CANDIDATES`` and
#: is repeated here so configuration validation cannot widen it.
MAX_CONFIGURED_CANDIDATES = 8

#: Score floor bounds. A configured floor outside this range is refused and the
#: default is used instead, so a typo cannot disable abstention.
_MIN_SCORE_FLOOR = 0.0
_MAX_SCORE_FLOOR = 1.0

#: The application-owned default REVIEWED design-library project id. A fixed
#: application constant, never derived from user input. Validated against the
#: library's own project-id grammar before use (see ``_parse_library_project_id``).
DEFAULT_DESIGN_LIBRARY_PROJECT_ID = "wb-design"


class DecisionEngineMode(str, Enum):
    """A Decision Engine mode. A closed set.

    Subclasses ``str`` so a value serialises as a plain string in state and
    configuration without a custom encoder.
    """

    DISABLED = "disabled"
    SHADOW = "shadow"
    ACTIVE = "active"


#: Every valid mode, as plain strings, for validation and tests.
DECISION_ENGINE_MODES = tuple(mode.value for mode in DecisionEngineMode)

#: The mode that leaves production behaviour untouched.
DEFAULT_DECISION_ENGINE_MODE = DecisionEngineMode.DISABLED


def parse_mode(value: Any) -> DecisionEngineMode:
    """Parse a configured mode, defaulting safely to ``disabled``.

    Accepts a :class:`DecisionEngineMode` or a case-insensitive string. Anything
    else — ``None``, a number, an unknown word, a whitespace-padded typo — yields
    ``disabled``. A configuration error must never turn the engine on.
    """
    if isinstance(value, DecisionEngineMode):
        return value
    if isinstance(value, str):
        normalised = value.strip().lower()
        for mode in DecisionEngineMode:
            if mode.value == normalised:
                return mode
    return DEFAULT_DECISION_ENGINE_MODE


@dataclass(frozen=True)
class DecisionEngineConfig:
    """Feature configuration for the Decision Engine (N6).

    DISABLED by default in both modes, so a builder or intake processor
    constructed without this config behaves exactly as it did before N6.

    ``intake_provider`` / ``design_provider`` name the provider in the registry.
    ``None`` (the default) selects the registry's own fallback (the deterministic
    provider), which is always available.
    """

    intake_mode: DecisionEngineMode = DEFAULT_DECISION_ENGINE_MODE
    design_mode: DecisionEngineMode = DEFAULT_DECISION_ENGINE_MODE
    intake_provider: Optional[str] = None
    design_provider: Optional[str] = None
    score_floor: float = 0.5
    max_candidates: int = 4
    #: The application-owned REVIEWED design-library project scope. This is a
    #: fixed application constant (or operator configuration), NEVER derived from
    #: user input, so a user can never redirect design retrieval to an unrelated
    #: project. Mirrors :attr:`app.core.laya_context.LayaConfig.library_project_id`.
    design_library_project_id: str = DEFAULT_DESIGN_LIBRARY_PROJECT_ID

    # -- N8: OpenRouter Decisions models -----------------------------------
    #
    # Both default to ``None``, which means "no Decisions provider is
    # registered". Combined with the default ``disabled`` modes above, a
    # deployment that sets neither key behaves exactly as before N8: the
    # Decision Engine is not even constructed (``_build_decision_engine``
    # returns ``None``).
    #
    # The model ids are the PINNED identities selected by the N8 benchmark, not
    # floating aliases, so a provider-side model update cannot silently change
    # what staging runs.
    intake_model: Optional[str] = None
    design_model: Optional[str] = None
    #: Decisions endpoint. Overridable for a gateway, never derived from input.
    decisions_base_url: str = "https://openrouter.ai/api/alpha/decisions"
    #: Bounded operational deadline for ONE Decisions call, in seconds.
    decisions_timeout_seconds: float = 20.0

    @property
    def intake_enabled(self) -> bool:
        """True when the engine is consulted for intake at all (shadow or active)."""
        return self.intake_mode is not DecisionEngineMode.DISABLED

    @property
    def design_enabled(self) -> bool:
        """True when the engine is consulted for design at all (shadow or active)."""
        return self.design_mode is not DecisionEngineMode.DISABLED

    @property
    def intake_active(self) -> bool:
        return self.intake_mode is DecisionEngineMode.ACTIVE

    @property
    def design_active(self) -> bool:
        return self.design_mode is DecisionEngineMode.ACTIVE


def _parse_provider(value: Any) -> Optional[str]:
    """Return a trimmed provider name, or ``None`` for anything unusable."""
    if isinstance(value, str):
        name = value.strip()
        return name or None
    return None


def _parse_library_project_id(value: Any) -> str:
    """Return a validated library project id, or the application default.

    The value is operator configuration (never user input), but it is still
    validated against the library's own project-id grammar so a typo cannot
    point retrieval at an out-of-grammar or unsafe scope. Anything unusable
    falls back to the fixed application default.
    """
    candidate = value.strip() if isinstance(value, str) else ""
    if not candidate:
        return DEFAULT_DESIGN_LIBRARY_PROJECT_ID
    try:
        from app.core.openviking_library import is_valid_project_id
    except Exception:  # pragma: no cover - import guard; library is always present
        return DEFAULT_DESIGN_LIBRARY_PROJECT_ID
    return candidate if is_valid_project_id(candidate) else DEFAULT_DESIGN_LIBRARY_PROJECT_ID


def _parse_score_floor(value: Any) -> float:
    """Return a floor in ``[0, 1]``, or the 0.5 default for anything unusable."""
    try:
        floor = float(value)
    except (TypeError, ValueError):
        return 0.5
    if floor < _MIN_SCORE_FLOOR or floor > _MAX_SCORE_FLOOR:
        return 0.5
    return floor


def _parse_max_candidates(value: Any) -> int:
    """Return a candidate cap clamped to ``[0, MAX_CONFIGURED_CANDIDATES]``."""
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 4
    return max(0, min(count, MAX_CONFIGURED_CANDIDATES))


def _parse_optional_model(value: Any) -> Optional[str]:
    """Return a Decisions model id, or ``None`` to leave the role disabled.

    Model ids are operator configuration, never user input. A blank or
    non-string value disables the role rather than guessing a model -- an
    accidental activation is the failure mode worth preventing.
    """
    candidate = value.strip() if isinstance(value, str) else ""
    if not candidate:
        return None
    # A model id is "<vendor>/<name>"; reject anything that cannot be one.
    if "/" not in candidate or any(ch.isspace() for ch in candidate):
        return None
    return candidate


def _parse_base_url(value: Any) -> str:
    """Return a validated Decisions endpoint, or the documented default.

    Only https endpoints are accepted. The URL is operator configuration and is
    never derived from a message, so this is a typo guard, not an input filter.
    """
    candidate = value.strip() if isinstance(value, str) else ""
    if candidate.startswith("https://") and " " not in candidate:
        return candidate
    return "https://openrouter.ai/api/alpha/decisions"


def _parse_timeout(value: Any) -> float:
    """Return a bounded per-call deadline in seconds.

    Clamped to a sane window: below 1s would fail on healthy calls, above 120s
    would let a stalled peer hold a turn longer than any interactive budget.
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return 20.0
    if seconds <= 0:
        return 20.0
    return max(1.0, min(seconds, 120.0))


def config_from_mapping(
    data: Optional[Mapping[str, Any]],
) -> DecisionEngineConfig:
    """Build a :class:`DecisionEngineConfig` from a parsed YAML mapping.

    Only documented keys are read. Every failure mode — a missing block, a
    malformed mode string, an out-of-range number — yields a safe value, and a
    malformed mode always yields ``disabled``. Nothing here can turn the engine
    on by accident.
    """
    data = data or {}
    return DecisionEngineConfig(
        intake_mode=parse_mode(data.get("intake_mode")),
        design_mode=parse_mode(data.get("design_mode")),
        intake_provider=_parse_provider(data.get("intake_provider")),
        design_provider=_parse_provider(data.get("design_provider")),
        score_floor=_parse_score_floor(data.get("score_floor", 0.5)),
        max_candidates=_parse_max_candidates(data.get("max_candidates", 4)),
        design_library_project_id=_parse_library_project_id(
            data.get("design_library_project_id")
        ),
        intake_model=_parse_optional_model(data.get("intake_model")),
        design_model=_parse_optional_model(data.get("design_model")),
        decisions_base_url=_parse_base_url(data.get("decisions_base_url")),
        decisions_timeout_seconds=_parse_timeout(
            data.get("decisions_timeout_seconds", 20.0)
        ),
    )


__all__ = [
    "MAX_CONFIGURED_CANDIDATES",
    "DEFAULT_DESIGN_LIBRARY_PROJECT_ID",
    "DecisionEngineMode",
    "DECISION_ENGINE_MODES",
    "DEFAULT_DECISION_ENGINE_MODE",
    "parse_mode",
    "DecisionEngineConfig",
    "config_from_mapping",
]
