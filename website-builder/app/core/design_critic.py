"""Impeccable critic seam (Batch D3a.5 Part I).

Turns the **real** Impeccable ``detect`` output into the ``CriticFinding``
schema D1 declared but nothing produced.

**Why this module exists and why it is narrow.** D1 declared ``CriticFinding``
and explicitly refused to write a parser for a resource that was absent, because
a parser for an uninspected resource is an invented contract. Part I closes
that gap only because the contract is now *verified against the source*, not
the docs: the provisioned Hermes skill ships its own bundled engine under
``scripts/bin/<os>-<arch>/impeccable`` and its launcher documents

    detect --json      emit findings as JSON on stdout, human text on stderr

with the exit codes that this module depends on:

    0   clean          (or nothing to scan)
    2   findings       (the normal "there is something to fix" answer)
    1   scan failure   (an honest failure, NOT an empty result)

**Exit 1 is deliberately NOT treated as "no findings."** Collapsing a failed
scan into a clean report would let a broken critic silently certify a design.
The failure is surfaced as :data:`REASON_SCAN_FAILED` so the caller can degrade
instead of claiming a pass it never earned.

**What this module does NOT do.** There is **no repair path**. No
``--fix``, no ``audit --apply``, no write-back of any kind. D3a.5 deliberately
excludes the D3b repair loop; a critic that can edit would be that loop. The
argv below is a fixed constant and the caller cannot extend it.

**What is NOT faked.**

* The engine path is resolved through the closed platform mapping in
  :mod:`app.core.design_activation` and must be a contained, existing regular
  file under the skill root. A missing engine is a *reported* absence, not a
  fabricated empty report.
* The argv is :data:`CRITIC_ARGV_SUFFIX` and nothing else. No caller-supplied
  flags, no path arguments, no shell. ``shell=True`` is never used, so a path
  containing shell metacharacters is an argv element, never a command.
* JSON that does not parse, or that is not the expected shape, yields zero
  findings plus a static reason. It is never coerced into an empty pass.
"""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.core.design_install import is_contained
from app.core.design_retrieval import CriticFinding

logger = logging.getLogger(__name__)

#: The engine's ``detect`` subcommand with machine-readable output. **Fixed**:
#: the caller may not add flags, paths, or targets. This is the reason the seam
#: is safe -- there is no argument through which a caller can steer the engine.
CRITIC_ARGV_SUFFIX: Tuple[str, ...] = ("detect", "--json", "--quiet")

#: Exit codes, as documented by the skill's own contract.
EXIT_CLEAN = 0
EXIT_FINDINGS = 2
EXIT_SCAN_FAILED = 1

#: Severity vocabulary the engine may report, mapped onto the canonical one.
#: An unknown severity is NOT coerced -- it is preserved verbatim so the caller
#: can see that upstream introduced a level this build does not understand.
_SEVERITIES = ("blocker", "critical", "warning", "note")

#: Bounded output. The engine scans a project; an unbounded finding list would
#: flow straight into a prompt.
MAX_FINDINGS = 50
MAX_FIELD_CHARS = 400

CRITIC_REASONS = (
    "engine_unavailable",
    "engine_not_executable",
    "scan_failed",
    "output_unparseable",
    "output_unexpected",
    "output_empty",
    "truncated",
)


@dataclass(frozen=True)
class CriticOutcome:
    """The result of one bounded critic scan."""

    ok: bool
    findings: Tuple[CriticFinding, ...] = ()
    truncated: bool = False
    reasons: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for reason in self.reasons:
            if reason not in CRITIC_REASONS:
                raise ValueError(f"unknown critic reason: {reason!r}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "findings": [finding.to_dict() for finding in self.findings],
            "truncated": self.truncated,
            "reasons": list(self.reasons),
        }


def resolve_engine_path(skill_root: Path, engine_relative: str) -> Optional[Path]:
    """The engine executable, or ``None`` if it is absent or uncontained.

    Containment is checked against the skill root, not merely the project: the
    engine executes, so a path that resolves outside the provisioned skill is
    refused before anything runs. Returns ``None`` -- never a best guess.
    """
    if not engine_relative:
        return None
    candidate = Path(skill_root) / engine_relative
    try:
        if not candidate.is_file():
            return None
        if not is_contained(Path(skill_root), candidate):
            logger.warning("Refused an Impeccable engine outside the skill root.")
            return None
    except OSError:
        return None
    return candidate


def build_critic_argv(engine_path: Path) -> Tuple[str, ...]:
    """The complete, fixed argv for one scan.

    No caller-supplied fragment appears here, and the executable is an argv
    element rather than a shell word -- so a path with spaces or metacharacters
    is handled by the OS, not by a shell.
    """
    return (str(engine_path),) + CRITIC_ARGV_SUFFIX


def _bound(text: object, limit: int = MAX_FIELD_CHARS) -> str:
    """A string field, coerced to text and bounded."""
    if text is None:
        return ""
    if isinstance(text, (int, float, bool)):
        return str(text)
    if not isinstance(text, str):
        return ""
    return text.strip()[:limit]


def _first_text(document: Mapping[str, Any], *names: str) -> str:
    """The first present, non-empty string among ``names``.

    Upstream has used more than one key for the same concept; reading a small
    closed set of aliases is a tolerance for that, not an open-ended lookup
    into whatever shape arrives.
    """
    for name in names:
        value = document.get(name)
        if isinstance(value, str) and value.strip():
            return _bound(value)
    return ""


def _normalize_severity(value: object) -> str:
    text = _bound(value).lower()
    return text if text in _SEVERITIES else _bound(value).lower()


def normalize_finding(document: Any) -> Optional[CriticFinding]:
    """One engine finding as a :class:`CriticFinding`, or ``None``.

    ``None`` for anything that is not a mapping carrying at least a rule
    identity and a description. Inventing an anonymous finding from a blob of
    JSON would put a claim into the prompt that the engine never made.
    """
    if not isinstance(document, Mapping):
        return None

    finding = _first_text(document, "finding", "message", "title", "description")
    rule_id = _first_text(document, "rule_id", "ruleId", "id", "rule", "code")
    if not finding or not rule_id:
        return None

    return CriticFinding(
        rule_id=rule_id,
        category=_first_text(document, "category", "group", "area") or "general",
        severity=_normalize_severity(document.get("severity") or document.get("level")),
        finding=finding,
        evidence=_first_text(document, "evidence", "excerpt", "detail", "snippet"),
        suggested_action=_first_text(
            document, "suggested_action", "suggestedAction", "remediation", "fix"
        ),
    )


def _payload_documents(payload: Any) -> Optional[List[Any]]:
    """The finding list inside the engine's JSON payload.

    Accepts the shapes the engine is known to emit: a bare array of findings, or
    an object under ``findings``/``issues``/``violations``/``results``. Anything
    else is *unexpected*, which is reported -- never treated as an empty list.
    """
    if isinstance(payload, list):
        return payload
    if isinstance(payload, Mapping):
        for key in ("findings", "issues", "violations", "results"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return None
    return None


def parse_critic_output(stdout: str, returncode: int) -> CriticOutcome:
    """The engine's ``(stdout, exit code)`` as a bounded, honest outcome.

    Exit-code handling is the load-bearing part:

    * ``0`` -- a clean scan. Findings may still be reported if the engine chose
      to emit them; the exit code is not used to invent or discard data.
    * ``2`` -- findings. The normal answer.
    * anything else -- a **scan failure**, reported as such. Not an empty pass.

    Output that does not parse is likewise a failure, never an empty pass.
    """
    if returncode not in (EXIT_CLEAN, EXIT_FINDINGS):
        return CriticOutcome(ok=False, reasons=("scan_failed",))

    text = stdout or ""
    if not text.strip():
        # Exit 0 with no output is a genuine clean scan and needs no JSON.
        if returncode == EXIT_CLEAN:
            return CriticOutcome(ok=True)
        return CriticOutcome(ok=False, reasons=("output_empty",))

    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return CriticOutcome(ok=False, reasons=("output_unparseable",))

    documents = _payload_documents(payload)
    if documents is None:
        return CriticOutcome(ok=False, reasons=("output_unexpected",))

    findings: List[CriticFinding] = []
    for document in documents:
        normalized = normalize_finding(document)
        if normalized is not None:
            findings.append(normalized)

    truncated = len(findings) > MAX_FINDINGS
    if truncated:
        findings = findings[:MAX_FINDINGS]

    reasons: List[str] = ["truncated"] if truncated else []
    return CriticOutcome(
        ok=True,
        findings=tuple(findings),
        truncated=truncated,
        reasons=tuple(reasons),
    )


def run_critic_scan(
    engine_path: Optional[Path],
    *,
    skill_root: Optional[Path] = None,
    timeout_seconds: int = 120,
    runner: Any = None,
) -> CriticOutcome:
    """Run one bounded ``detect`` and return its outcome. **No repair path.**

    ``runner`` is injectable so tests can drive every exit-code and
    malformed-output branch without executing anything. The default runner is
    :func:`subprocess.run` with ``shell=False`` -- the engine path is an argv
    element, never a shell word.

    An unresolvable engine is a *reported* absence. Returning
    ``ok=True`` with zero findings there would certify a design that was never
    scanned.
    """
    if engine_path is None:
        return CriticOutcome(ok=False, reasons=("engine_unavailable",))

    argv = build_critic_argv(engine_path)
    cwd = str(skill_root) if skill_root is not None else None

    execute = runner or subprocess.run
    try:
        completed = execute(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            shell=False,
        )
    except subprocess.TimeoutExpired:
        return CriticOutcome(ok=False, reasons=("scan_failed",))
    except (OSError, ValueError) as error:
        logger.warning("Impeccable critic could not be executed: %s", type(error).__name__)
        return CriticOutcome(ok=False, reasons=("engine_not_executable",))

    return parse_critic_output(
        getattr(completed, "stdout", "") or "", getattr(completed, "returncode", 1)
    )


__all__ = [
    "CRITIC_ARGV_SUFFIX",
    "CRITIC_REASONS",
    "EXIT_CLEAN",
    "EXIT_FINDINGS",
    "EXIT_SCAN_FAILED",
    "MAX_FIELD_CHARS",
    "MAX_FINDINGS",
    "CriticOutcome",
    "build_critic_argv",
    "normalize_finding",
    "parse_critic_output",
    "resolve_engine_path",
    "run_critic_scan",
]