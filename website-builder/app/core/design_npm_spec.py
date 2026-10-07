"""Bounded npm package-spec parsing for the registry dependency contract (D3a.5).

Upstream registry metadata declares its dependencies as **npm package specs**,
not bare names: the live React Bits ``SplitText-TS-TW`` document declares

    "dependencies": ["gsap@^3.13.0", "@gsap/react@^2.1.2"]

The previous resolver mapped the WHOLE string through
``PACKAGE_TO_DEPENDENCY_ID.get(name.strip())``. That cannot understand a spec at
all, so a *versioned* declaration silently failed to map and the component's real
requirements were lost -- the ``@gsap/react`` half of SplitText simply vanished.

This module is the narrow, application-owned parser that closes that gap. It is
deliberately NOT a general npm implementation:

* It understands exactly the grammar the reviewed contract needs -- a bare or
  versioned, scoped or unscoped package identity:

      gsap
      gsap@^3.13.0
      gsap@3.15.0
      @gsap/react
      @gsap/react@^2.1.2

* Everything else fails closed. npm aliases (``npm:other``), git/ssh hosts
  (``git+https://``, ``github:user/repo``), http(s)/file/link/workspace
  protocols, dist-tags (``latest``, ``next``), wildcards (``*``), and malformed
  scoped specs are all REFUSED, because each is a way for a remote registry to
  redirect an install at something this application never reviewed.

**The parsed constraint is a CONSTRAINT, never an install instruction.** It is
used only to check that the application-owned exact pin satisfies what upstream
asked for. ``^3.13.0`` is never forwarded to a package manager; the exact pin is.
See :func:`constraint_is_satisfied`.

**Untrusted input is never echoed.** A rejected spec yields a static label from
:data:`REJECTED_SPEC_LABEL`, so a bounded operational reason cannot carry
attacker-controlled text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Tuple

# ---------------------------------------------------------------------------
# Static, bounded labels -- never the rejected input
# ---------------------------------------------------------------------------

#: The label used wherever a spec could not be parsed. Static by construction:
#: the raw string is never copied into a reason, a log line, or a report field.
REJECTED_SPEC_LABEL = "<unsupported-spec>"


# ---------------------------------------------------------------------------
# Grammar -- a CLOSED, bounded subset
# ---------------------------------------------------------------------------

#: npm package names are lowercase; scoped names keep their ``@scope/name``
#: shape. This is the SAME shape :func:`app.core.design_install.package_name_is_well_formed`
#: enforces, kept here so the parser is self-contained and cannot accept a name
#: the installer would then reject.
_PACKAGE_NAME_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")

#: One version constraint token: an optional operator plus a dotted version,
#: optionally carrying a prerelease/build suffix. No tags, no ``*``, no ``x``.
_CONSTRAINT_TOKEN = r"(?:\^|~|>=|<=|>|<|=)?\d+(?:\.\d+){0,2}(?:-[0-9A-Za-z.-]+)?"

#: A full constraint: one or more comparator tokens joined by whitespace (AND),
#: optionally several such clauses joined by ``||`` (OR). Bounded and anchored,
#: so ``latest``, ``next``, ``*``, ``file:...`` and every other unsupported form
#: fail to match.
_CONSTRAINT_RE = re.compile(
    rf"^{_CONSTRAINT_TOKEN}(?:\s+{_CONSTRAINT_TOKEN})*"
    rf"(?:\s*\|\|\s*{_CONSTRAINT_TOKEN}(?:\s+{_CONSTRAINT_TOKEN})*)*$"
)

#: Characters that mark a NON-registry install source. A spec containing any of
#: these is refused outright, before the grammar is even consulted, so a scheme
#: (``git+https://``, ``file:``, ``link:``, ``workspace:``, ``npm:``) can never
#: be smuggled in through a version slot. ``/`` is deliberately NOT listed: a
#: scoped name legitimately contains exactly one, and the package-name regex
#: below is what bounds that usage.
_FORBIDDEN_SPEC_CHARS = (":", "\\", " ", "\t", "\n", "\r")

_OPERATORS = (">=", "<=", ">", "<", "=", "^", "~")


@dataclass(frozen=True)
class NpmPackageSpec:
    """A parsed, bounded npm package specification.

    ``declared_constraint`` is the upstream RANGE (``""`` for a bare name). It is
    a check, not an instruction: the installer only ever runs the
    application-owned exact pin.
    """

    package_name: str
    declared_constraint: str = ""

    @property
    def is_bare(self) -> bool:
        """Whether upstream declared a bare name with no version constraint."""
        return not self.declared_constraint

    def to_dict(self) -> dict:
        """Serializable. Both halves are validated, so neither is untrusted text."""
        return {
            "package_name": self.package_name,
            "declared_constraint": self.declared_constraint,
        }


def _split_name_and_constraint(text: str) -> Tuple[str, str, bool]:
    """Split ``name[@constraint]`` on the version separator.

    Returns ``(name, constraint, had_separator)``. The separator is the ``@``
    after the package name -- NOT the leading scope sigil, which a scoped name
    (``@gsap/react``) carries at index 0. ``had_separator`` distinguishes a bare
    name (``gsap``) from an explicitly empty constraint (``gsap@``), because npm
    reads the latter as ``latest`` and that must fail closed.
    """
    if text.startswith("@"):
        index = text.find("@", 1)
    else:
        index = text.find("@")
    if index == -1:
        return text, "", False
    return text[:index], text[index + 1 :], True


def parse_npm_package_spec(raw: object) -> Optional[NpmPackageSpec]:
    """Parse ``raw`` into an :class:`NpmPackageSpec`, or return ``None``.

    ``None`` is the fail-closed answer for anything outside the reviewed grammar:
    a non-string, an empty string, a package name that is not a valid npm name,
    a constraint that is not a bounded version range, an empty constraint after
    an explicit ``@`` (which npm reads as ``latest``), or any spec carrying a
    protocol/alias/tag marker. The caller refuses the whole component on ``None``
    rather than widening the allowlist or forwarding the raw string.
    """
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not text:
        return None

    # A scheme, an alias, a path, or embedded whitespace is never a registry
    # package spec this application installs. Refused before parsing.
    for forbidden in _FORBIDDEN_SPEC_CHARS:
        if forbidden in text:
            return None

    name, constraint, had_separator = _split_name_and_constraint(text)
    if not name or not _PACKAGE_NAME_RE.match(name):
        return None
    if had_separator and not constraint:
        # `gsap@` is npm's spelling for the floating `latest` tag.
        return None
    if constraint and not _CONSTRAINT_RE.match(constraint):
        return None
    return NpmPackageSpec(package_name=name, declared_constraint=constraint)


# ---------------------------------------------------------------------------
# Constraint satisfaction -- bounded semver comparison
# ---------------------------------------------------------------------------


def _parse_version(value: str) -> Optional[Tuple[int, int, int, str]]:
    """``major.minor.patch[-prerelease]`` -> ``(maj, min, pat, pre)`` or ``None``."""
    if not isinstance(value, str):
        return None
    match = re.match(r"^(\d+)(?:\.(\d+))?(?:\.(\d+))?(?:-([0-9A-Za-z.-]+))?$", value.strip())
    if match is None:
        return None
    major = int(match.group(1))
    minor = int(match.group(2) or 0)
    patch = int(match.group(3) or 0)
    prerelease = match.group(4) or ""
    return major, minor, patch, prerelease


def _compare(left: Tuple[int, int, int, str], right: Tuple[int, int, int, str]) -> int:
    """Compare two parsed versions. Prerelease sorts BEFORE the release."""
    for a, b in zip(left[:3], right[:3]):
        if a != b:
            return -1 if a < b else 1
    left_pre, right_pre = left[3], right[3]
    if left_pre == right_pre:
        return 0
    if not left_pre:
        return 1
    if not right_pre:
        return -1
    return -1 if left_pre < right_pre else 1


def _satisfies_clause(clause: str, version: Tuple[int, int, int, str]) -> bool:
    """Whether ``version`` satisfies every whitespace-joined comparator."""
    for token in clause.split():
        operator = ""
        for candidate in _OPERATORS:
            if token.startswith(candidate):
                operator = candidate
                token = token[len(candidate) :]
                break
        bound = _parse_version(token)
        if bound is None:
            return False
        comparison = _compare(version, bound)
        if operator == ">=" and comparison < 0:
            return False
        if operator == ">" and comparison <= 0:
            return False
        if operator == "<=" and comparison > 0:
            return False
        if operator == "<" and comparison >= 0:
            return False
        if operator in ("=", "") and comparison != 0:
            return False
        if operator == "^":
            if comparison < 0:
                return False
            # ^a.b.c := >=a.b.c <(a+1).0.0, except ^0.b.c and ^0.0.c.
            if bound[0] > 0:
                ceiling = (bound[0] + 1, 0, 0, "")
            elif bound[1] > 0:
                ceiling = (0, bound[1] + 1, 0, "")
            else:
                ceiling = (0, 0, bound[2] + 1, "")
            if _compare(version, ceiling) >= 0:
                return False
        if operator == "~":
            if comparison < 0:
                return False
            # ~a.b.c := >=a.b.c <a.(b+1).0
            ceiling = (bound[0], bound[1] + 1, 0, "")
            if _compare(version, ceiling) >= 0:
                return False
    return True


def constraint_is_satisfied(constraint: str, version: str) -> bool:
    """Whether the exact ``version`` satisfies the upstream ``constraint``.

    Used ONLY to prove the application's exact pin is inside the range upstream
    declared. A malformed or empty constraint returns ``False`` -- the caller
    treats that as a refused component, never as a satisfied one, so a parse gap
    fails closed rather than silently accepting an unverifiable pin.
    """
    if not isinstance(constraint, str) or not constraint.strip():
        return False
    if not _CONSTRAINT_RE.match(constraint.strip()):
        return False
    parsed = _parse_version(version)
    if parsed is None:
        return False
    for clause in constraint.split("||"):
        if _satisfies_clause(clause.strip(), parsed):
            return True
    return False


__all__ = [
    "REJECTED_SPEC_LABEL",
    "NpmPackageSpec",
    "constraint_is_satisfied",
    "parse_npm_package_spec",
]
