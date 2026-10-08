"""Impeccable critic seam (Batch D3a.5 Part I).

Turns the **real** Impeccable ``detect`` output into the ``CriticFinding``
schema D1 declared but nothing produced.

**Why this module exists and why it is narrow.** D1 declared ``CriticFinding``
and explicitly refused to write a parser for a resource that was absent, because
a parser for an uninspected resource is an invented contract. Part I closes
that gap only because the contract is now *verified against the official
release*, not the docs: pbakaus/impeccable ``skill-v4.1.0`` publishes one
asset, ``universal.zip``
(sha256:a54d837f086ff2036ab0cf2cc2499249362d38fa27d42feb07d6248dfdd64a11),
whose Hermes layout contains NO ``scripts/bin/`` and no native binary at all.
The detector is a Node ESM entrypoint, ``scripts/detect.mjs``, whose own
``--help`` documents

    detect --json --quiet    emit findings as JSON on stdout

and whose shipped source fixes the exit codes this module depends on:

    0   clean          (or nothing to scan)
    2   findings       (the normal "there is something to fix" answer)
    1   scan failure   (an honest failure, NOT an empty result)

The JSON payload is a **bare array**, and each finding's real field names are
``antipattern`` / ``name`` / ``description`` / ``severity`` / ``category`` /
``file`` / ``line`` / ``snippet`` -- read from the shipped ``findings.mjs``
factory, not guessed. Older key aliases are still accepted, because a parser
that dropped an upstream rename would silently report zero findings.

**Exit 1 is deliberately NOT treated as "no findings."** Collapsing a failed
scan into a clean report would let a broken critic silently certify a design.
The failure is surfaced as :data:`REASON_SCAN_FAILED` so the caller can degrade
instead of claiming a pass it never earned.

**What this module does NOT do.** There is **no repair path**. No
``--fix``, no ``audit --apply``, no write-back of any kind. D3a.5 deliberately
excludes the D3b repair loop; a critic that can edit would be that loop. The
argv below is a fixed constant and the caller cannot extend it.

**What is NOT faked.**

* The engine path is resolved through
  :func:`app.core.design_activation.resolve_engine_path` and must be a
  contained, existing, non-empty regular file under the skill root. A missing
  engine is a *reported* absence, not a fabricated empty report.
* The interpreter is the Node executable the caller supplies. It is never
  searched for on ``PATH`` here, never installed, and never shimmed from inside
  the project -- an unvetted interpreter is exactly the "npm shim" this batch
  refused.
* The engine flags are :data:`CRITIC_ENGINE_ARGV_SUFFIX` and nothing else. No
  caller-supplied flags, no caller-supplied target paths, no shell. The one
  target is a FIXED ``.``: without it the engine reads STDIN on a non-TTY and
  returns a clean verdict over a project it never opened (see the constant's
  comment). ``shell=True`` is never used, so a path containing shell
  metacharacters is an argv element, never a command.
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

from app.core.design_activation import resolve_engine_path as _resolve_engine_path
from app.core.design_install import is_contained
from app.core.design_retrieval import CriticFinding

logger = logging.getLogger(__name__)

#: The engine's ``detect`` subcommand with machine-readable output, and a FIXED
#: ``.`` scan target. **Fixed**: the caller may not add flags, paths, or targets
#: -- there is no argument through which a caller can steer the engine.
#:
#: The ``.`` target is LOAD-BEARING, not decorative. The engine's own CLI
#: (``detector/cli/main.mjs``) reads:
#:
#:     if (!process.stdin.isTTY && targets.length === 0) {
#:       allFindings = await handleStdin(...)      # reads STDIN
#:     } else {
#:       const paths = targets.length > 0 ? targets : [process.cwd()]
#:
#: With NO target and a non-TTY stdin -- every subprocess, daemon, and CI run --
#: the engine reads an EMPTY stdin and returns ``[]`` with exit 0. That is a
#: clean, non-degraded, "authoritative" verdict over a project it never opened:
#: exactly the "certify a design that was never scanned" failure this module
#: exists to prevent. Passing ``.`` selects the cwd branch unconditionally, so
#: the scan is the same whether or not a TTY is attached.
#:
#: ``--quiet`` only suppresses the human summary on stderr; the JSON payload and
#: the exit code are unaffected, and those are what this module reads.
CRITIC_ENGINE_ARGV_SUFFIX: Tuple[str, ...] = ("detect", "--json", "--quiet", ".")

#: Backwards-compatible alias for the flag suffix. It names the FLAGS passed to
#: the engine, never the engine path.
CRITIC_ARGV_SUFFIX: Tuple[str, ...] = CRITIC_ENGINE_ARGV_SUFFIX

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
    "parser_runtime_unavailable",
)


#: The exact marker the shipped ``detect.mjs`` writes to STDERR when the parser
#: modules are missing and it falls back to regex. Verified in the skill-v4.1.0
#: artifact (``detector/engines/static-html/detect-html.mjs``). Detecting it is
#: what lets a caller tell a full-quality scan from an undercount -- a degraded
#: clean result must never be read as authoritative.
DEGRADED_MARKER = "DEGRADED - HTML parser modules unavailable"


@dataclass(frozen=True)
class CriticOutcome:
    """The result of one bounded critic scan."""

    ok: bool
    findings: Tuple[CriticFinding, ...] = ()
    truncated: bool = False
    reasons: Tuple[str, ...] = ()
    #: True when the engine ran but reported its DEGRADED (regex-fallback) path,
    #: so the findings are an UNDERCOUNT and a clean result is NOT authoritative.
    #: Distinct from ``ok``: the scan succeeded, but at reduced quality.
    degraded: bool = False

    def __post_init__(self) -> None:
        for reason in self.reasons:
            if reason not in CRITIC_REASONS:
                raise ValueError(f"unknown critic reason: {reason!r}")

    @property
    def authoritative(self) -> bool:
        """Whether a clean result may be trusted as full-quality.

        False whenever the engine degraded, because a degraded clean scan is an
        undercount, not a pass.
        """
        return self.ok and not self.degraded

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "findings": [finding.to_dict() for finding in self.findings],
            "truncated": self.truncated,
            "reasons": list(self.reasons),
            "degraded": self.degraded,
            "authoritative": self.authoritative,
        }


#: The engine resolver is owned by :mod:`app.core.design_activation`, which
#: encodes the VERIFIED upstream layout (a cross-platform Node ESM entrypoint and
#: the detector facade it imports). It is re-exported here rather than
#: reimplemented: two copies of "where the engine lives" would drift, and the
#: drifted copy is exactly what made the previous revision report a correctly
#: provisioned skill as unavailable.
resolve_engine_path = _resolve_engine_path


def build_critic_argv(node_executable: Any, engine_path: Path) -> Tuple[str, ...]:
    """The complete, fixed argv for one scan.

    The shipped engine is a Node ESM module, so argv[0] is the interpreter and
    argv[1] is the verified entrypoint. Neither is searched for on ``PATH``
    here, and no caller-supplied fragment appears beyond the interpreter and the
    already-resolved engine path. Both are argv elements rather than shell
    words, so a path with spaces or metacharacters is handled by the OS.
    """
    return (
        str(node_executable),
        str(engine_path),
    ) + CRITIC_ENGINE_ARGV_SUFFIX


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

    # The shipped `findings.mjs` factory stamps the rule id as `antipattern`;
    # `rule_id`/`id`/`code` are the older or alternate spellings.
    finding = _first_text(document, "description", "finding", "message", "title")
    rule_id = _first_text(
        document, "antipattern", "rule_id", "ruleId", "id", "rule", "code"
    )
    if not finding or not rule_id:
        return None

    # A real finding carries a human `name`; use it as a readable prefix.
    display = _first_text(document, "name", "title")

    return CriticFinding(
        rule_id=rule_id,
        category=_first_text(document, "category", "group", "area") or "general",
        severity=_normalize_severity(
            document.get("severity") or document.get("level")
        ),
        finding=finding if not display else f"{display}: {finding}",
        # `snippet` is the shipped evidence field; `file`/`line` locate it.
        evidence=_first_text(
            document, "snippet", "evidence", "excerpt", "detail"
        ),
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
    node_executable: Optional[str] = None,
    skill_root: Optional[Path] = None,
    timeout_seconds: int = 120,
    runner: Any = None,
) -> CriticOutcome:
    """Run one bounded ``detect`` and return its outcome. **No repair path.**

    ``node_executable`` is REQUIRED and is never discovered. Looking one up on
    ``PATH`` would mean silently running whatever interpreter happens to be
    installed, and ``npx``/an npm shim would download one into the project --
    both refused by this batch. A caller that cannot name an interpreter reports
    an absent engine rather than guessing at one.

    ``runner`` is injectable so tests can drive every exit-code and
    malformed-output branch without executing anything. The default runner is
    :func:`subprocess.run` with ``shell=False`` -- every argument is an argv
    element, never a shell word.

    An unresolvable engine is a *reported* absence. Returning
    ``ok=True`` with zero findings there would certify a design that was never
    scanned.
    """
    if engine_path is None or not isinstance(node_executable, str) or not node_executable.strip():
        return CriticOutcome(ok=False, reasons=("engine_unavailable",))

    argv = build_critic_argv(node_executable, engine_path)
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

    outcome = parse_critic_output(
        getattr(completed, "stdout", "") or "", getattr(completed, "returncode", 1)
    )

    # The engine writes its DEGRADED notice to STDERR. When it does, the scan ran
    # at reduced quality: the findings are an undercount, so a clean result is
    # NOT authoritative. This is reported, never silently swallowed.
    stderr = getattr(completed, "stderr", "") or ""
    if DEGRADED_MARKER in stderr:
        reasons = tuple(sorted(set(outcome.reasons) | {"parser_runtime_unavailable"}))
        return CriticOutcome(
            ok=outcome.ok,
            findings=outcome.findings,
            truncated=outcome.truncated,
            reasons=reasons,
            degraded=True,
        )
    return outcome


def scan_is_authoritative(outcome: CriticOutcome) -> bool:
    """Whether ``outcome`` is a full-quality, authoritative result.

    A degraded scan is NOT authoritative even when ``ok``: its clean result is an
    undercount. This is the single predicate a caller should gate "treat the
    critic as having certified this design" on.
    """
    return outcome.authoritative


__all__ = [
    "CRITIC_ARGV_SUFFIX",
    "CRITIC_ENGINE_ARGV_SUFFIX",
    "CRITIC_REASONS",
    "DEGRADED_MARKER",
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
    "scan_is_authoritative",
]