"""R2 release identity and publication-stage records for Website Builder.

This module is the single authority for the R2 release contract:

    PREPARED -> GIT_CONFIRMED -> PRODUCTION_CONFIRMED -> SMOKE_PASSED -> COMMITTED

Three properties are load-bearing and are enforced here rather than at the
call sites that happen to remember them.

**1. One linear stage graph.** There is no second graph for the
publication-not-configured case. When publication is not configured the
machine still advances ``PREPARED -> GIT_CONFIRMED``, as a local no-op with
zero Git subprocesses, and ``publication.status`` carries ``NOT_CONFIGURED``.
Resume, reconciliation and failure bookkeeping therefore have exactly one code
path and never have to ask which graph a record came from.

**2. Release identity is bound in application state, not derived from a branch.**
``deployment.last_live_release`` is the authoritative LIVE release. The Git
branch is a publication HISTORY; its head says what was published, never what
is live. ``deployment.last_live_deployment`` survives only as a derived
compatibility projection of the same write.

**3. Two explicit completeness modes.** A release this system committed under
R2 is ``COMPLETE`` and must carry the full identity contract. State lazily
derived from a pre-R2 record is ``LEGACY_PARTIAL``: only the facts R1 actually
persisted, with everything else left ``null`` rather than inferred. A partial
record is a valid *previous* release (a rollback target, "what was live
before") and is never a valid *new* release identity.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional
from urllib.parse import urlsplit

from app.core.contracts import StaleOperationIntent
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore

# ---------------------------------------------------------------------------
# Stage graph
# ---------------------------------------------------------------------------

STAGE_PREPARED = "PREPARED"
STAGE_GIT_CONFIRMED = "GIT_CONFIRMED"
STAGE_PRODUCTION_CONFIRMED = "PRODUCTION_CONFIRMED"
STAGE_SMOKE_PASSED = "SMOKE_PASSED"
STAGE_COMMITTED = "COMMITTED"

#: The only legal order. ``COMMITTED`` is terminal: the record is cleared and
#: replaced by ``last_live_release``.
STAGE_ORDER = (
    STAGE_PREPARED,
    STAGE_GIT_CONFIRMED,
    STAGE_PRODUCTION_CONFIRMED,
    STAGE_SMOKE_PASSED,
    STAGE_COMMITTED,
)

# ``publication.status`` values. NOT_CONFIGURED is a STATUS, not a stage: it
# occupies the normal GIT_CONFIRMED slot so the graph never forks.
PUBLICATION_PENDING = "PENDING"
PUBLICATION_CONFIRMED = "CONFIRMED"
PUBLICATION_NOT_CONFIGURED = "NOT_CONFIGURED"
PUBLICATION_FAILED = "FAILED"

PUBLICATION_STATUSES = (
    PUBLICATION_PENDING,
    PUBLICATION_CONFIRMED,
    PUBLICATION_NOT_CONFIGURED,
    PUBLICATION_FAILED,
)

OUTCOME_TERMINAL_FAILED = "TERMINAL_FAILED"
OUTCOME_RECONCILIATION_REQUIRED = "RECONCILIATION_REQUIRED"
OUTCOMES = (None, OUTCOME_TERMINAL_FAILED, OUTCOME_RECONCILIATION_REQUIRED)

# Reconciliation verdicts, mirroring the A/B/C/D matrix. C and D are distinct
# on purpose: C means the remote was READ and holds an unexpected state, D
# means the remote could not be read and we therefore assert nothing.
VERDICT_A_ADOPT = "A_ADOPT"
VERDICT_B_RETRY = "B_RETRY"
VERDICT_C_CONFLICT = "C_CONFLICT"
VERDICT_D_UNAVAILABLE = "D_UNAVAILABLE"

# ---------------------------------------------------------------------------
# Error codes
# ---------------------------------------------------------------------------

ERROR_HEAD_CONFLICT = "PUBLICATION_HEAD_CONFLICT"
ERROR_HEAD_UNREADABLE = "PUBLICATION_HEAD_UNREADABLE"
ERROR_PUSH_FAILED = "PUBLICATION_PUSH_FAILED"
ERROR_SUPERSEDE_FORBIDDEN = "PUBLICATION_SUPERSEDE_FORBIDDEN"
ERROR_NO_TRUSTED_TESTED_COMMIT = "NO_TRUSTED_TESTED_COMMIT"
#: A LOCAL publication input is unusable (an intended commit or parent that is
#: not a commit id, or a publication target that vanished). Not a C/D verdict:
#: nothing was read, so nothing is asserted about the branch. Kept in step with
#: ``app.deploy.git_output.PUBLICATION_INPUT_INVALID``.
ERROR_PUBLICATION_INPUT_INVALID = "PUBLICATION_INPUT_INVALID"

#: Verdict -> error code. Authoritative, and never collapsed: a transport
#: failure is D/UNREADABLE, never C/CONFLICT.
VERDICT_ERROR_CODES = {
    VERDICT_C_CONFLICT: ERROR_HEAD_CONFLICT,
    VERDICT_D_UNAVAILABLE: ERROR_HEAD_UNREADABLE,
}

# ---------------------------------------------------------------------------
# Completeness
# ---------------------------------------------------------------------------

COMPLETENESS_COMPLETE = "COMPLETE"
COMPLETENESS_LEGACY_PARTIAL = "LEGACY_PARTIAL"

#: Validation modes. ``new_release`` is what ``commit_release`` uses and only
#: ever accepts a COMPLETE record; ``previous_release`` is the read-side
#: compatibility role and additionally accepts a LEGACY_PARTIAL one.
MODE_NEW_RELEASE = "new_release"
MODE_PREVIOUS_RELEASE = "previous_release"
VALIDATION_MODES = (MODE_NEW_RELEASE, MODE_PREVIOUS_RELEASE)

_SHA1_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
# Mirrors ``_FRIENDLY_BRANCH_RE`` in app.deploy.git_output: a single lower-case
# ref path component, never the internal preview/<hash>/<hash> ref.
_BRANCH_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")

RELEASE_FIELDS = (
    "release_id", "source_revision", "operation_id",
    "tested_commit", "tested_tree",
    "publication_commit", "publication_tree", "publication_parent",
    "publication_branch", "publication_repo",
    "source_sha256", "artifact_sha256",
    "deployment_id", "production_url", "deployment_url",
    "smoke", "committed_at", "completeness", "publication_configured",
)

#: The release record is SELF-DESCRIBING: it says whether publication was
#: configured for the operation that produced it, rather than leaving every
#: reader to infer it from the presence or absence of ``publication_commit``.
#:
#: That inference is unsafe in both directions. A ``NOT_CONFIGURED`` release is
#: COMPLETE with a null publication commit, and a lazily derived
#: ``LEGACY_PARTIAL`` record may carry a commit while lacking everything else --
#: so ``publication_commit`` distinguishes neither, and a validator that guesses
#: from it rejects releases this system legitimately committed.
#:
#: Persisting the fact is safe because a stored record cannot *decide* anything
#: with it: ``commit_release`` re-derives the fact from the on-disk pending
#: publication inside the writer lock and refuses any disagreement. The flag is
#: corroboration for readers, not authority for the writer.
PUBLICATION_CONFIGURED_FIELD = "publication_configured"

#: The five publication-identity fields R2 requires for a configured release,
#: plus the two tested-identity fields. All seven are null by design when
#: publication was not configured, and all seven are required when it was.
PUBLICATION_IDENTITY_FIELDS = (
    "publication_commit", "publication_tree", "publication_parent",
    "publication_repo", "publication_branch",
)
UNCONFIGURED_IDENTITY_FIELDS = PUBLICATION_IDENTITY_FIELDS + (
    "tested_commit", "tested_tree",
)

#: Fields R1 never persisted. A lazily derived record must leave every one of
#: these ``None`` -- an unknown identity is unknown, never inferred, and never
#: fabricated to make a record look complete.
LEGACY_UNKNOWN_FIELDS = (
    "publication_tree", "publication_parent", "publication_repo",
    "publication_branch", "tested_commit", "tested_tree",
    "source_sha256", "artifact_sha256",
)


class ReleaseRecordError(ValueError):
    """A release record violates the R2 release identity contract."""


class ReleaseStageError(ValueError):
    """An attempted stage transition is not the next legal stage."""


# ---------------------------------------------------------------------------
# Stage ordering
# ---------------------------------------------------------------------------


def stage_index(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        raise ReleaseStageError(f"Unknown publication stage: {stage!r}") from None


def next_stage(stage: str) -> str:
    """The one stage that may follow *stage*.

    Raises for ``COMMITTED``, which is terminal, and for anything unknown.
    """
    index = stage_index(stage)
    if index + 1 >= len(STAGE_ORDER):
        raise ReleaseStageError(f"Stage {stage!r} is terminal")
    return STAGE_ORDER[index + 1]


def assert_stage_advances(current: str, target: str) -> None:
    """*target* must be exactly the stage after *current*.

    Regressions and skips are both refused. A NOT_CONFIGURED record is not an
    exception: it holds the same stage values as a configured one, so there is
    no branch of this function for it.
    """
    expected = next_stage(current)
    if target != expected:
        raise ReleaseStageError(
            f"Illegal publication stage transition: {current} -> {target} "
            f"(expected {expected})"
        )


def at_least(stage: str, target: str) -> bool:
    """True when *stage* is *target* or later in the graph."""
    return stage_index(stage) >= stage_index(target)


# ---------------------------------------------------------------------------
# Field validation
# ---------------------------------------------------------------------------


def _require(cond: bool, message: str) -> None:
    if not cond:
        raise ReleaseRecordError(message)


def _is_sha1(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA1_RE.fullmatch(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_RE.fullmatch(value))


def _is_number(value: Any) -> bool:
    # bool is an int subclass; a timestamp is never True.
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _validate_publication_url(value: Any, field: str) -> str:
    """An https URL with a host, no embedded credentials, and no whitespace.

    Deliberately not narrowed to a single hostname: the canonical production
    URL is a vercel.app host, and the deployment-specific URL is a different
    vercel.app host. What is refused is anything that could smuggle a
    credential (``user:pass@``) or unbounded payload into persisted state.
    """
    _require(isinstance(value, str) and value.startswith("https://"),
             f"{field} must be an https URL")
    _require(len(value) <= 2048, f"{field} is unreasonably long")
    _require(not any(c.isspace() for c in value), f"{field} contains whitespace")
    parts = urlsplit(value)
    _require(bool(parts.hostname), f"{field} has no host")
    _require(parts.username is None and parts.password is None,
             f"{field} must not embed credentials")
    return value


def _validate_smoke(smoke: Any, production_url: str) -> Dict[str, Any]:
    """The production-smoke evidence a committed release must carry.

    The smoke must have PASSED, and it must have been performed against the
    canonical production host -- otherwise the evidence describes some other
    URL than the one the release claims to be live at.
    """
    _require(isinstance(smoke, dict), "smoke evidence must be an object")
    _require(smoke.get("status") == "PASSED", "smoke did not pass")
    _require(_is_number(smoke.get("at")) and smoke["at"] > 0,
             "smoke.at must be a positive timestamp")
    _require(smoke.get("failure_classification") is None,
             "a passed smoke must carry no failure classification")
    host = smoke.get("target_host")
    _require(isinstance(host, str) and bool(host), "smoke.target_host is required")
    path = smoke.get("target_path")
    _require(isinstance(path, str) and path.startswith("/"),
             "smoke.target_path must be an absolute path")
    _require(host == urlsplit(production_url).hostname,
             "smoke target host does not match the production URL host")
    return smoke


def validate_last_live_release(record: Any, mode: str, *,
                              publication_configured: Optional[bool] = None,
                              ) -> Dict[str, Any]:
    """Validate one ``last_live_release`` record under an explicit *mode*.

    ``mode`` is ``new_release`` or ``previous_release``; there is no inferred
    or best-effort mode.

    ``publication_configured`` is the *operation's* fact about whether
    publication was configured, supplied by the caller that still holds the
    pending record (``build_last_live_release`` and ``commit_release`` both do).
    When it is omitted -- a read-side check, where no pending record survives
    COMMITTED -- it is read from the record's own ``publication_configured``
    field, which every R2 record persists.

    The flag is never inferred from ``publication_commit``. Two of the three
    possible shapes make that inference wrong: a ``NOT_CONFIGURED`` release is
    COMPLETE with a null commit, and a ``LEGACY_PARTIAL`` one may carry a
    commit while missing everything else. A COMPLETE record must therefore
    carry a real ``bool``, and a record that does not is refused rather than
    guessed at -- an unverifiable publication identity stays unknown.

    ``new_release``
        What ``commit_release`` must produce. Only ``COMPLETE`` is accepted,
        and every required identity field must be present and well-formed.

    ``previous_release``
        The read-side compatibility role -- rollback targets and "what was
        live before". Accepts ``COMPLETE`` (fully validated) and additionally
        ``LEGACY_PARTIAL``, whose only requirement is that every field R1 never
        persisted is still ``None``.

    Returns the record on success so a validated copy can be persisted without
    a second look.
    """
    _require(mode in VALIDATION_MODES, f"Unknown validation mode: {mode!r}")
    _require(isinstance(record, dict), "release record must be an object")
    completeness = record.get("completeness")
    _require(completeness in (COMPLETENESS_COMPLETE, COMPLETENESS_LEGACY_PARTIAL),
             "release record has no valid completeness")

    if completeness == COMPLETENESS_LEGACY_PARTIAL:
        if mode == MODE_NEW_RELEASE:
            raise ReleaseRecordError(
                "A legacy partial record is not a valid new release identity"
            )
        for field in LEGACY_UNKNOWN_FIELDS:
            _require(record.get(field) is None,
                     f"Legacy partial record must not carry {field}")
        _require(isinstance(record.get("operation_id"), str)
                 and bool(record.get("operation_id")),
                 "Legacy partial record requires an operation_id")
        # R1 could prove publication was attempted (it left a repository
        # record) or prove nothing at all (it never did). It could NEVER prove
        # that publication was absent, so ``False`` is not a legacy value and
        # a record carrying it is fabricating certainty R1 did not have.
        legacy_configured = record.get(PUBLICATION_CONFIGURED_FIELD)
        _require(legacy_configured in (True, None),
                 "A legacy partial record may only prove publication was "
                 "configured, or record that it could not be known")
        return record

    # ---- COMPLETE, both modes.
    _require(isinstance(record.get("release_id"), str) and bool(record.get("release_id")),
             "release_id is required")
    _require(isinstance(record.get("operation_id"), str) and bool(record.get("operation_id")),
             "operation_id is required")
    _require(isinstance(record.get("source_revision"), int)
             and not isinstance(record.get("source_revision"), bool)
             and record["source_revision"] >= 1,
             "source_revision must be a positive integer")
    _require(_is_sha256(record.get("source_sha256")), "source_sha256 must be a sha256")
    _require(_is_sha256(record.get("artifact_sha256")), "artifact_sha256 must be a sha256")
    _require(_is_number(record.get("committed_at")) and record["committed_at"] > 0,
             "committed_at must be a positive timestamp")

    deployment_id = record.get("deployment_id")
    _require(isinstance(deployment_id, str) and bool(deployment_id),
             "deployment_id is required")
    _require(len(deployment_id) <= 256, "deployment_id is unreasonably long")
    production_url = _validate_publication_url(record.get("production_url"),
                                               "production_url")
    _validate_publication_url(record.get("deployment_url"), "deployment_url")
    _validate_smoke(record.get("smoke"), production_url)

    # Whether publication was configured is a fact about the OPERATION. The
    # caller that still holds the pending record supplies it; otherwise the
    # record's own persisted fact is read. It is never guessed from the shape
    # of the publication identity.
    if publication_configured is None:
        _require(PUBLICATION_CONFIGURED_FIELD in record,
                 "A complete release must record whether publication was "
                 "configured")
        publication_configured = record[PUBLICATION_CONFIGURED_FIELD]
        _require(isinstance(publication_configured, bool),
                 "publication_configured must be a bool")
    if publication_configured:
        # A configured release carries the full tested identity AND the full
        # publication identity. Both are proven: one by the approved snapshot,
        # the other by the confirmed push.
        _require(_is_sha1(record.get("tested_commit")), "tested_commit must be a commit id")
        _require(_is_sha1(record.get("tested_tree")), "tested_tree must be a tree id")
        _require(_is_sha1(record.get("publication_commit")),
                 "publication_commit must be a commit id")
        _require(_is_sha1(record.get("publication_tree")),
                 "publication_tree must be a tree id")
        parent = record.get("publication_parent")
        _require(parent is None or _is_sha1(parent),
                 "publication_parent must be a commit id or null")
        _require(isinstance(record.get("publication_repo"), str)
                 and bool(_REPO_RE.fullmatch(record.get("publication_repo") or "")),
                 "publication_repo must be owner/name")
        branch = record.get("publication_branch")
        _require(isinstance(branch, str) and bool(_BRANCH_RE.fullmatch(branch))
                 and not branch.startswith("preview/"),
                 "publication_branch must be a friendly branch name")
    else:
        # An unconfigured release has no Git identity AT ALL: there was no
        # tested snapshot commit to bind and nothing was published. Those seven
        # fields are null by design, and the record is still COMPLETE because
        # everything the operation actually did is present.
        for field in UNCONFIGURED_IDENTITY_FIELDS:
            _require(record.get(field) is None,
                     f"an unconfigured release must leave {field} null")
    return record


def release_is_known(record: Any) -> bool:
    """Whether *record* describes the current LIVE release with full identity.

    Callers that need a complete identity must ask this rather than testing for
    the presence of ``publication_commit``: a NOT_CONFIGURED release is
    COMPLETE with a null publication commit, and a LEGACY_PARTIAL one may
    carry a commit while still lacking everything else.

    It works for both because the record states its own
    ``publication_configured`` fact. A NOT_CONFIGURED release is recognised as
    a known identity; a LEGACY_PARTIAL one is not, because the fields R1 never
    persisted are still missing and no amount of re-reading invents them.
    """
    if not isinstance(record, dict):
        return False
    try:
        validate_last_live_release(record, MODE_PREVIOUS_RELEASE)
    except ReleaseRecordError:
        return False
    return record.get("completeness") == COMPLETENESS_COMPLETE


# ---------------------------------------------------------------------------
# Record builders
# ---------------------------------------------------------------------------


def resolve_branch_parent(state: Any) -> Optional[str]:
    """The commit the next publication commit must chain onto.

    That is ``deployment.publication_head.commit``: the last publication commit
    CONFIRMED on the remote branch, whatever later became of its release. It is
    deliberately not the LIVE release's publication commit -- a release that
    was confirmed on Git but never reached production still owns the branch, and
    the next publication must chain onto it rather than collide with it.

    Returns ``None`` for the first publication (a root commit).
    """
    head = (getattr(state, "deployment", None) or {}).get("publication_head") or {}
    commit = head.get("commit")
    if commit is None:
        return None
    if not _is_sha1(commit):
        raise ReleaseRecordError("publication_head.commit is not a commit id")
    return commit


def build_pending_publication(
    *, operation_id: str, source_revision: int, source_sha256: str,
    artifact_sha256: str, now: Optional[float] = None,
    prepared: Optional[Dict[str, Any]] = None, parent: Optional[str] = None,
    branch: Optional[str] = None, repo: Optional[str] = None,
) -> Dict[str, Any]:
    """The durable in-flight publication record written at PREPARED.

    Two shapes, one record:

    * publication configured -- ``prepared`` is the identity returned by
      ``OutputGitRepository.prepare_publication``, already built and validated
      with no network access, and ``publication.status`` is PENDING.
    * publication not configured -- ``prepared`` is None and
      ``publication.status`` is NOT_CONFIGURED, with every Git identity field
      left ``None`` because there is nothing to record, not because something
      failed.
    """
    _require(isinstance(operation_id, str) and bool(operation_id),
             "pending publication requires an operation_id")
    _require(isinstance(source_revision, int) and source_revision >= 1,
             "pending publication requires a positive source_revision")
    _require(_is_sha256(source_sha256), "pending publication source_sha256 must be a sha256")
    _require(_is_sha256(artifact_sha256), "pending publication artifact_sha256 must be a sha256")
    stamp = time.time() if now is None else now
    if prepared is None:
        publication = {
            "configured": False,
            "repo": None,
            "branch": None,
            "tested_commit": None,
            "tested_tree": None,
            "intended_commit": None,
            "intended_parent": parent,
            "intended_tree": None,
            "status": PUBLICATION_NOT_CONFIGURED,
            "confirmed_at": None,
        }
    else:
        publication = {
            "configured": True,
            "repo": prepared["repo"],
            "branch": prepared["branch"],
            "tested_commit": prepared["tested_commit"],
            "tested_tree": prepared["tested_tree"],
            "intended_commit": prepared["publication_commit"],
            "intended_parent": prepared["parent"],
            "intended_tree": prepared["tree"],
            "status": PUBLICATION_PENDING,
            "confirmed_at": None,
        }
        if publication["intended_parent"] != parent:
            # The prepared identity and the resolved parent must be the same
            # fact, or the commit we are about to push was built against a
            # different history than the one we hold as branch authority.
            raise ReleaseRecordError(
                "Prepared publication parent does not match publication_head"
            )
        if publication["branch"] != branch:
            raise ReleaseRecordError(
                "Prepared publication branch does not match the resolved branch"
            )
        if publication["repo"] != repo:
            raise ReleaseRecordError(
                "Prepared publication repo does not match the configured remote"
            )
    return {
        "operation_id": operation_id,
        "source_revision": source_revision,
        "stage": STAGE_PREPARED,
        "outcome": None,
        "reconciliation_required": False,
        "created_at": stamp,
        "updated_at": stamp,
        "source_sha256": source_sha256,
        "artifact_sha256": artifact_sha256,
        "publication": publication,
        "production": {
            "deployment_id": None,
            "promoted_deployment_id": None,
            "confirmed_at": None,
        },
        "smoke": {"status": None, "at": None},
        "last_error_code": None,
    }


def build_last_live_release(
    *, operation_id: str, source_revision: int, source_sha256: str,
    artifact_sha256: str, deployment_id: str, production_url: str,
    deployment_url: str, smoke: Dict[str, Any], publication: Dict[str, Any],
    now: Optional[float] = None,
) -> Dict[str, Any]:
    """The authoritative LIVE release record, validated before it is returned.

    The caller supplies the *pending publication* record; this reads the
    confirmed publication identity straight out of it rather than taking
    publication facts from anywhere else, so the committed release can never
    disagree with the publication that was actually confirmed.

    ``publication_configured`` is persisted. The release record is the only
    thing that outlives the pending record, so if it does not carry this fact
    then every later reader has to guess it from the shape of the publication
    identity -- and a NOT_CONFIGURED release is COMPLETE with a null commit, so
    that guess is wrong exactly when it matters. Persisting it is not the same
    as letting the record decide anything: ``commit_release`` re-derives the
    fact from the on-disk pending publication inside the writer lock and
    refuses a record that disagrees.
    """
    configured = publication.get("configured") is True
    record = {
        "release_id": operation_id,
        "source_revision": source_revision,
        "operation_id": operation_id,
        "tested_commit": publication.get("tested_commit") if configured else None,
        "tested_tree": publication.get("tested_tree") if configured else None,
        "publication_commit": publication.get("intended_commit") if configured else None,
        "publication_tree": publication.get("intended_tree") if configured else None,
        "publication_parent": publication.get("intended_parent") if configured else None,
        "publication_branch": publication.get("branch") if configured else None,
        "publication_repo": publication.get("repo") if configured else None,
        "source_sha256": source_sha256,
        "artifact_sha256": artifact_sha256,
        "deployment_id": deployment_id,
        "production_url": production_url,
        "deployment_url": deployment_url,
        "smoke": smoke,
        "committed_at": time.time() if now is None else now,
        "completeness": COMPLETENESS_COMPLETE,
        PUBLICATION_CONFIGURED_FIELD: configured,
    }
    return validate_last_live_release(
        record, MODE_NEW_RELEASE, publication_configured=configured)


def legacy_live_release(last_live_deployment: Dict[str, Any],
                        repository: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Derive a LEGACY_PARTIAL release from a pre-R2 on-disk record.

    Preserves only what R1 actually persisted. Every field R1 never wrote stays
    ``None`` -- in particular ``publication_tree``, ``tested_tree``, the
    source/artifact hashes and any smoke evidence. A legacy record is a
    previous-release identity only; it is never a new release and is never
    upgraded to COMPLETE by reading it.

    ``publication_configured`` is recorded as the tri-state R1 can actually
    support, which is not a bool:

    * ``True``  -- R1 left a repository record naming a repo or a branch, or a
      publication commit. R1 wrote that record only when it had a configured
      publication and attempted it, so this is proven.
    * ``None``  -- R1 left no repository record at all. It never wrote one for a
      "no deploy key configured" install, and it never wrote one for a live
      release that predates source publication, so the absence proves nothing
      either way. This is recorded as an explicit unknown, never guessed.

    ``False`` is not a legacy value: R1 had no way to record "publication was
    not configured", so any record carrying it is fabricating certainty the
    historical state never had. ``validate_last_live_release`` enforces that.
    """
    repository = repository if isinstance(repository, dict) else {}
    record = {
        "release_id": last_live_deployment.get("operation_id"),
        "source_revision": last_live_deployment.get("source_revision"),
        "operation_id": last_live_deployment.get("operation_id"),
        "tested_commit": None,
        "tested_tree": None,
        "publication_commit": repository.get("publication_commit"),
        "publication_tree": None,
        "publication_parent": None,
        "publication_branch": None,
        "publication_repo": None,
        "source_sha256": None,
        "artifact_sha256": None,
        "deployment_id": last_live_deployment.get("deployment_id"),
        "production_url": last_live_deployment.get("production_url"),
        "deployment_url": last_live_deployment.get("deployment_url"),
        "smoke": {"status": None, "at": None},
        "committed_at": last_live_deployment.get("live_at"),
        "completeness": COMPLETENESS_LEGACY_PARTIAL,
        PUBLICATION_CONFIGURED_FIELD: _legacy_publication_configured(repository),
    }
    validate_last_live_release(record, MODE_PREVIOUS_RELEASE)
    return record


# ---------------------------------------------------------------------------
# Canonical source (R2-C)
# ---------------------------------------------------------------------------
#
# A revision may only start from a source whose identity is PROVEN. These
# verdicts are the admission gate: they say whether
# ``deployment.last_live_release`` names an exact, well-formed publication
# that can be hydrated byte-for-byte, and they never touch the filesystem.
#
# ``canonical_source_verdict`` is a PURE READ. It computes no cache, writes
# nothing, and takes no lock; ``reserve()`` is its only caller. The reason is
# not tidiness: a read path that wrote would make an admission check into a
# mutation, and a refusal must leave durable state byte-equivalent.
#
# Retrying never helps any of these. They are properties of durable state, not
# transient conditions, so the user-facing copy must never say "try again".

CANONICAL_SOURCE_NO_LIVE_RELEASE = "CANONICAL_SOURCE_NO_LIVE_RELEASE"
CANONICAL_SOURCE_SYNC_REQUIRED = "CANONICAL_SOURCE_SYNC_REQUIRED"
CANONICAL_SOURCE_LEGACY_IDENTITY = "CANONICAL_SOURCE_LEGACY_IDENTITY"
CANONICAL_SOURCE_NOT_PUBLISHED = "CANONICAL_SOURCE_NOT_PUBLISHED"
CANONICAL_SOURCE_COMMIT_MISSING = "CANONICAL_SOURCE_COMMIT_MISSING"
CANONICAL_SOURCE_PARENT_UNRESOLVED = "CANONICAL_SOURCE_PARENT_UNRESOLVED"
CANONICAL_SOURCE_REPO_UNRESOLVED = "CANONICAL_SOURCE_REPO_UNRESOLVED"
CANONICAL_SOURCE_REPO_MISMATCH = "CANONICAL_SOURCE_REPO_MISMATCH"

#: Verdict names. ``READY`` is the only one that admits a LIVE revision.
VERDICT_NO_LIVE_RELEASE = "NO_LIVE_RELEASE"
VERDICT_SOURCE_SYNC_REQUIRED = "SOURCE_SYNC_REQUIRED"
VERDICT_LEGACY_RELEASE_IDENTITY = "LEGACY_RELEASE_IDENTITY"
VERDICT_PUBLICATION_NOT_CONFIGURED = "PUBLICATION_NOT_CONFIGURED"
VERDICT_NO_PUBLICATION_COMMIT = "NO_PUBLICATION_COMMIT"
VERDICT_PUBLICATION_PARENT_UNRESOLVED = "PUBLICATION_PARENT_UNRESOLVED"
VERDICT_REMOTE_IDENTITY_UNRESOLVED = "REMOTE_IDENTITY_UNRESOLVED"
VERDICT_REPO_MISMATCH = "REPO_MISMATCH"
VERDICT_READY = "READY"

#: R1's exact "publication was attempted and never synced" evidence. The
#: comparison is an exact string on purpose: any other value (``SYNCED``, an
#: unrecognised status) is not this evidence and falls through to the general
#: legacy verdict rather than being read as a claim.
LEGACY_SYNC_REQUIRED = "SOURCE_SYNC_REQUIRED"


class CanonicalSourceRefusal(ValueError):
    """A LIVE revision's canonical source is not admissible.

    Carries the stable error code so ``reserve()`` and the user-facing copy
    both read the same classification instead of re-deriving it.
    """

    def __init__(self, error_code: str, message: str = ""):
        self.error_code = error_code
        super().__init__(message or error_code)


@dataclass(frozen=True)
class CanonicalSourceVerdict:
    """One admission verdict. Pure data, no side effects."""

    verdict: str
    error_code: Optional[str] = None

    @property
    def ready(self) -> bool:
        return self.verdict == VERDICT_READY


READY = CanonicalSourceVerdict(VERDICT_READY)


def _verdict(verdict: str, error_code: str) -> CanonicalSourceVerdict:
    return CanonicalSourceVerdict(verdict, error_code)


def _complete_release_admission(record: Dict[str, Any]) -> CanonicalSourceVerdict:
    """Every way a COMPLETE R2 release can still be unusable as a source.

    Nothing here is loosened relative to ``validate_last_live_release``; this
    only re-reads the same fields to name WHICH identity is missing, so the
    refusal is specific instead of a generic "not ready".

    ``publication_parent is None`` is VALID and is not refused: that is a root
    publication, which is the normal shape of a project's first LIVE release.
    Only a non-null parent that is not a commit id is unresolved.
    """
    if not _is_sha1(record.get("publication_commit")):
        return _verdict(VERDICT_NO_PUBLICATION_COMMIT,
                        CANONICAL_SOURCE_COMMIT_MISSING)
    parent = record.get("publication_parent")
    if parent is not None and not _is_sha1(parent):
        return _verdict(VERDICT_PUBLICATION_PARENT_UNRESOLVED,
                        CANONICAL_SOURCE_PARENT_UNRESOLVED)
    repo = record.get("publication_repo")
    if not isinstance(repo, str) or not _REPO_RE.fullmatch(repo):
        return _verdict(VERDICT_REMOTE_IDENTITY_UNRESOLVED,
                        CANONICAL_SOURCE_REPO_UNRESOLVED)
    branch = record.get("publication_branch")
    if (not isinstance(branch, str) or not _BRANCH_RE.fullmatch(branch)
            or branch.startswith("preview/")):
        return _verdict(VERDICT_REMOTE_IDENTITY_UNRESOLVED,
                        CANONICAL_SOURCE_REPO_UNRESOLVED)
    if not _is_sha1(record.get("publication_tree")):
        return _verdict(VERDICT_NO_PUBLICATION_COMMIT,
                        CANONICAL_SOURCE_COMMIT_MISSING)
    if not _is_sha1(record.get("tested_commit")) or not _is_sha1(record.get("tested_tree")):
        return _verdict(VERDICT_NO_PUBLICATION_COMMIT,
                        CANONICAL_SOURCE_COMMIT_MISSING)
    if not _is_sha256(record.get("source_sha256")) or not _is_sha256(
            record.get("artifact_sha256")):
        return _verdict(VERDICT_NO_PUBLICATION_COMMIT,
                        CANONICAL_SOURCE_COMMIT_MISSING)
    return READY


def canonical_source_verdict(state: Any) -> CanonicalSourceVerdict:
    """Whether a LIVE revision has an admissible exact source. Pure read.

    Locked precedence:

    1. ``NO_LIVE_RELEASE`` -- no release record at all.
    2. ``SOURCE_SYNC_REQUIRED`` -- R1 proved a publication was attempted and
       never synced. The R1 evidence is read from
       ``deployment.legacy_source_sync`` (projected by the lazy migration) and
       applies ONLY inside the ``LEGACY_PARTIAL`` branch.
    3. ``LEGACY_RELEASE_IDENTITY`` -- an R1 record with no more specific
       evidence.
    4. ``PUBLICATION_NOT_CONFIGURED`` -- a COMPLETE R2 release whose
       publication was never configured, so it has no Git identity to hydrate.
    5. A COMPLETE release whose identity is missing or malformed.
    6. ``READY``.

    The ``LEGACY_PARTIAL`` gate on 2 and 3 is load-bearing, and is defended
    twice over. A project that was R1 and later earned a genuine R2 COMPLETE
    release still carries whatever ``legacy_source_sync`` its R1 state left
    behind, so reading that evidence without the branch gate would refuse a
    perfectly good R2 release. The second defense is ``commit_release``, which
    removes the R1 evidence on the single successful COMPLETE COMMITTED save.
    """
    record = (getattr(state, "deployment", None) or {}).get("last_live_release")
    if not isinstance(record, dict):
        return _verdict(VERDICT_NO_LIVE_RELEASE, CANONICAL_SOURCE_NO_LIVE_RELEASE)
    completeness = record.get("completeness")

    if completeness == COMPLETENESS_LEGACY_PARTIAL:
        legacy_sync = (getattr(state, "deployment", None) or {}).get(
            "legacy_source_sync")
        if isinstance(legacy_sync, dict) and legacy_sync.get("sync_status") == LEGACY_SYNC_REQUIRED:
            return _verdict(VERDICT_SOURCE_SYNC_REQUIRED,
                            CANONICAL_SOURCE_SYNC_REQUIRED)
        return _verdict(VERDICT_LEGACY_RELEASE_IDENTITY,
                        CANONICAL_SOURCE_LEGACY_IDENTITY)

    if completeness != COMPLETENESS_COMPLETE:
        # Neither COMPLETE nor LEGACY_PARTIAL: the record states no usable
        # completeness, and an unrecognised one is never read as the strictest.
        return _verdict(VERDICT_NO_PUBLICATION_COMMIT,
                        CANONICAL_SOURCE_COMMIT_MISSING)

    if record.get(PUBLICATION_CONFIGURED_FIELD) is False:
        return _verdict(VERDICT_PUBLICATION_NOT_CONFIGURED,
                        CANONICAL_SOURCE_NOT_PUBLISHED)
    return _complete_release_admission(record)


def canonical_source_repo_verdict(state: Any,
                                  configured_repo: Optional[str]) -> CanonicalSourceVerdict:
    """Whether the recorded repo names the operator-configured remote.

    Pure and offline. Separate from ``canonical_source_verdict`` because
    comparing against configuration is not a read of durable project state.

    A project with a COMPLETE configured release but no configured remote
    cannot be compared here, and is not refused for it: the hydrate-time
    repository-identity check (``HYDRATION_REPO_MISMATCH``) is the authority
    there, and inventing a refusal here would duplicate it in a place that
    cannot see the configuration.
    """
    if not isinstance(configured_repo, str) or not configured_repo:
        return READY
    record = (getattr(state, "deployment", None) or {}).get("last_live_release")
    if not isinstance(record, dict):
        return READY
    if record.get("publication_repo") != configured_repo:
        return _verdict(VERDICT_REPO_MISMATCH, CANONICAL_SOURCE_REPO_MISMATCH)
    return READY


def _legacy_publication_configured(repository: Dict[str, Any]) -> Optional[bool]:
    """``True`` if R1 proves publication was configured, else ``None``.

    R1's ``_record_source_sync`` / ``_record_source_sync_required`` wrote a
    repository record only on an actual attempt, and each of those paths ran
    only when a remote, a branch and a deploy key were all present. A record
    naming any of those facts is therefore proof. An empty record proves
    nothing, and is reported as unknown rather than as "not configured".
    """
    if any(repository.get(key) for key in
           ("provider", "repo", "branch", "publication_commit", "sync_status")):
        return True
    return None

# ---------------------------------------------------------------------------
# Coordinator
# ---------------------------------------------------------------------------


class ReleaseCoordinator:
    """Every durable write of the R2 release contract.

    All stage writes are writer-locked, operation-bound, and fail closed with
    ``StaleOperationIntent`` when the record on disk belongs to a different
    operation. That is the same contract ``promote._update_intent`` applies to
    ``promotion_intent``: a side effect whose only durable evidence is this
    record must never proceed against someone else's record.
    """

    def __init__(self, store: ProjectStateStore):
        self.store = store

    # -- reads ---------------------------------------------------------

    def pending(self, state: Any) -> Optional[Dict[str, Any]]:
        record = (getattr(state, "deployment", None) or {}).get("pending_publication")
        return record if isinstance(record, dict) and record.get("operation_id") else None

    def is_reconciliation_required(self, state: Any) -> bool:
        pending = self.pending(state)
        return bool(pending and pending.get("reconciliation_required"))

    # -- writes --------------------------------------------------------

    def advance_stage(self, project_id: str, operation_id: str, stage: str,
                      *, production: Optional[Dict[str, Any]] = None,
                      smoke: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Advance the pending publication to exactly the next *stage*.

        Raises ``ReleaseStageError`` on a skip or regression and
        ``StaleOperationIntent`` when the record on disk is not this
        operation's.
        """
        with self.store.acquire_writer(project_id) as state:
            record = self._locked_pending(state, operation_id)
            assert_stage_advances(record.get("stage"), stage)
            record["stage"] = stage
            record["updated_at"] = time.time()
            if production is not None:
                record.setdefault("production", {}).update(production)
            if smoke is not None:
                record.setdefault("smoke", {}).update(smoke)
            self.store.save(state)
            return dict(record)

    def ensure_stage(self, project_id: str, operation_id: str, stage: str,
                     *, production: Optional[Dict[str, Any]] = None,
                     smoke: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Advance to *stage*, or accept that the record is already there.

        The resume-safe form of ``advance_stage``. A stage is a FACT the
        operation has reached, not an event it emits, so a same-operation
        resume that re-enters a stage an earlier process already completed must
        not be treated as an illegal transition.

        A record BEHIND *stage* advances (with the ordering guard still in
        force, so it can never skip). A record already AT *stage* is accepted
        and its evidence fields are updated. A record AHEAD of *stage* is a
        regression and still fails closed.
        """
        with self.store.acquire_writer(project_id) as state:
            record = self._locked_pending(state, operation_id)
            current = stage_index(record.get("stage"))
            target = stage_index(stage)
            if current < target:
                assert_stage_advances(record.get("stage"), stage)
                record["stage"] = stage
            elif current > target:
                raise ReleaseStageError(
                    f"Publication record at {record.get('stage')!r} cannot re-enter "
                    f"the earlier stage {stage!r}")
            record["updated_at"] = time.time()
            if production is not None:
                record.setdefault("production", {}).update(production)
            if smoke is not None:
                record.setdefault("smoke", {}).update(smoke)
            self.store.save(state)
            return dict(record)

    def confirm_publication(self, project_id: str, operation_id: str, *,
                            remote_head: Optional[str] = None) -> Dict[str, Any]:
        """Advance to GIT_CONFIRMED and advance the branch parent authority.

        Runs inside ONE locked write so ``publication_head`` can never claim a
        commit the pending record has not recorded, or vice versa. For a
        NOT_CONFIGURED record this is a local confirmation: the status is
        already NOT_CONFIGURED, the Git identity fields stay ``None``, and
        ``publication_head`` is deliberately left alone -- there is no new
        branch authority when nothing was published.
        """
        with self.store.acquire_writer(project_id) as state:
            record = self._locked_pending(state, operation_id)
            current = stage_index(record.get("stage"))
            if current < stage_index(STAGE_GIT_CONFIRMED):
                assert_stage_advances(record.get("stage"), STAGE_GIT_CONFIRMED)
                record["stage"] = STAGE_GIT_CONFIRMED
            publication = record.setdefault("publication", {})
            if publication.get("configured") is not True:
                publication["status"] = PUBLICATION_NOT_CONFIGURED
                publication["confirmed_at"] = time.time()
            else:
                commit = publication.get("intended_commit")
                if not _is_sha1(commit):
                    raise ReleaseRecordError(
                        "Cannot confirm a publication with no intended commit")
                publication["status"] = PUBLICATION_CONFIRMED
                publication["confirmed_at"] = time.time()
                confirmed = remote_head if remote_head is not None else commit
                if confirmed != commit:
                    # A confirmed publication IS the intended commit. Recording
                    # anything else would make the branch authority a guess.
                    raise ReleaseRecordError(
                        "Confirmed publication head does not match the intended commit")
                state.deployment["publication_head"] = {
                    "commit": commit,
                    "branch": publication.get("branch"),
                    "confirmed_at": time.time(),
                }
            # A successful confirmation supersedes whatever an earlier attempt
            # at this same stage recorded. Leaving a stale TERMINAL_FAILED
            # outcome in place would make the later COMMITTED write refuse a
            # release whose Git stage in fact succeeded.
            record["outcome"] = None
            record["reconciliation_required"] = False
            record["last_error_code"] = None
            record["updated_at"] = time.time()
            self.store.save(state)
            return dict(record)

    def mark_terminal_failure(self, project_id: str, operation_id: str,
                              error_code: str, *, publication_failed: bool = False,
                              smoke: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Record a conclusive failure at the current stage.

        The stage is NOT advanced: the record still says how far the operation
        actually got, which is exactly what a later resume needs to decide
        where to pick up.
        """
        with self.store.acquire_writer(project_id) as state:
            record = self._locked_pending(state, operation_id)
            record["outcome"] = OUTCOME_TERMINAL_FAILED
            record["last_error_code"] = error_code
            record["updated_at"] = time.time()
            if publication_failed:
                record.setdefault("publication", {})["status"] = PUBLICATION_FAILED
            if smoke is not None:
                record.setdefault("smoke", {}).update(smoke)
            self.store.save(state)
            return dict(record)

    def mark_reconciliation_required(self, project_id: str, operation_id: str,
                                     error_code: str) -> Dict[str, Any]:
        """Fail closed on an ambiguous Git state, holding PUBLISHING.

        Sets ``reconciliation_required``, which also blocks any NEW operation
        from superseding this one until an operator resolves it.
        """
        with self.store.acquire_writer(project_id) as state:
            record = self._locked_pending(state, operation_id)
            record["outcome"] = OUTCOME_RECONCILIATION_REQUIRED
            record["reconciliation_required"] = True
            record["last_error_code"] = error_code
            record["updated_at"] = time.time()
            self.store.save(state)
            return dict(record)

    def commit_release(self, project_id: str, *, operation_id: str, release: Dict[str, Any],
                       ) -> Dict[str, Any]:
        """The single atomic COMMITTED write.

        One writer-locked save performs every part of going live: the lifecycle
        transition, ``live_revision``, the canonical ``production_url``, the
        derived ``last_live_deployment`` projection, the authoritative
        ``last_live_release``, and the clearing of ``pending_publication``.

        The release record is validated in ``new_release`` mode first, so a
        partial record can never be committed as this system's LIVE release.
        Its persisted ``publication_configured`` is then cross-checked against
        the on-disk pending publication, so a record cannot declare itself
        unconfigured in order to pass, and the publication status must be a
        settled one (``CONFIRMED`` or ``NOT_CONFIGURED``) -- never ``PENDING``
        or ``FAILED``, neither of which is a statement the Git stage has made.
        """
        with self.store.acquire_writer(project_id) as state:
            pending = self._locked_pending(state, operation_id)
            publication = pending.get("publication") or {}
            configured = publication.get("configured") is True
            # Validate inside the lock: the publication fact this record is
            # judged against belongs to the on-disk pending record.
            record = validate_last_live_release(
                release, MODE_NEW_RELEASE, publication_configured=configured)
            if pending.get("stage") != STAGE_SMOKE_PASSED:
                raise ReleaseStageError(
                    f"Cannot commit a release from stage {pending.get('stage')!r}")
            # Two refusals, both about the PUBLICATION rather than about any
            # later stage: an unresolved Git ambiguity, or a publication the
            # Git stage itself failed. A recorded Vercel-level failure that a
            # later successful promote and smoke have overtaken is stale by
            # definition and is not a reason to refuse here.
            #
            # The status must be a SETTLED one. A COMMITTED release is proof
            # the publication was proven (CONFIRMED) or that there was nothing
            # to prove (NOT_CONFIGURED); PENDING and FAILED are both states in
            # which the Git stage has not vouched for the branch, and a release
            # committed from one of those would be a claim with nothing behind
            # it.
            if pending.get("reconciliation_required"):
                raise ReleaseStageError(
                    "Cannot commit a release whose publication is held for "
                    "reconciliation")
            if publication.get("status") not in (PUBLICATION_CONFIRMED,
                                                 PUBLICATION_NOT_CONFIGURED):
                raise ReleaseStageError(
                    f"Cannot commit a release with publication status "
                    f"{publication.get('status')!r}")
            if record.get("operation_id") != operation_id:
                raise ReleaseRecordError("Release record does not match the operation")
            # The record's own publication fact is corroboration for readers,
            # never authority: it must agree with the operation this write
            # commits, or the two records disagree about what happened.
            if record.get(PUBLICATION_CONFIGURED_FIELD) is not configured:
                raise ReleaseRecordError(
                    "Release publication_configured does not match the "
                    "operation's confirmed publication")
            if configured:
                for field, key in (
                    ("publication_commit", "intended_commit"),
                    ("publication_tree", "intended_tree"),
                    ("publication_parent", "intended_parent"),
                    ("publication_branch", "branch"),
                    ("publication_repo", "repo"),
                ):
                    if record.get(field) != publication.get(key):
                        raise ReleaseRecordError(
                            f"Release {field} does not match the confirmed publication")
            if state.lifecycle != ProjectLifecycle.PUBLISHING.value:
                raise ReleaseStageError(
                    f"Cannot commit a release from lifecycle {state.lifecycle!r}")
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.LIVE)
            state.revisions.live_revision = record["source_revision"]
            state.production_url = record["production_url"]
            # A resume arrives carrying the failure record of the attempt that
            # failed. LIVE is the outcome, so that record is now stale and must
            # not be left for an operator to misread.
            state.failure = None
            intent = state.deployment.get("promotion_intent")
            if isinstance(intent, dict) and intent.get("operation_id") == operation_id:
                intent["stage"] = "live"
                intent["canonical_production_url"] = record["production_url"]
                intent["deployment_url"] = record["deployment_url"]
            # Compatibility projection, derived from the authoritative record
            # rather than assembled separately, so the two cannot disagree.
            state.deployment["last_live_deployment"] = {
                "operation_id": record["operation_id"],
                "deployment_id": record["deployment_id"],
                "production_url": record["production_url"],
                "deployment_url": record["deployment_url"],
                "source_revision": record["source_revision"],
                "source_sha256": record["source_sha256"],
                "artifact_sha256": record["artifact_sha256"],
                "live_at": record["committed_at"],
            }
            state.deployment["last_live_release"] = dict(record)
            state.deployment.pop("pending_publication", None)
            # D8': the R1 "publication was attempted and never synced" evidence
            # is now STALE by construction. This save commits a genuine R2
            # COMPLETE release, so the record that follows can no longer be
            # refused for R1 evidence about a different generation of the
            # project. It is removed HERE, after every validation above
            # succeeded and in the same atomic write as the release itself --
            # a refused or failed commit raises before reaching this line, so
            # the evidence survives exactly as long as the refusal it explains.
            state.deployment.pop("legacy_source_sync", None)
            self.store.save(state)
        return dict(record)
    # -- internals -----------------------------------------------------

    @staticmethod
    def _locked_pending(state, operation_id: str) -> Dict[str, Any]:
        record = (state.deployment or {}).get("pending_publication")
        if not isinstance(record, dict) or record.get("operation_id") != operation_id:
            raise StaleOperationIntent(
                "pending_publication does not belong to this operation"
            )
        return record
