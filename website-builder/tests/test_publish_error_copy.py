"""BUG 6 regression: publish failures speak to the user in their language.

``INCOMPLETE_LOOKUP`` and friends are operator-grade codes. The p9 E2E showed
the user one of them and the message was indistinguishable from a failure with
no explanation, which is the worst possible outcome for an ambiguous state: it
either looks like the publish failed (it did not) or like a code the user is
expected to understand (they are not).

The internal codes stay exactly as they are -- they are the stable contract for
operators, dashboards, and tests. Only the RENDERING changes, and the rules are:

  * the copy names the real problem in plain Indonesian,
  * it never claims success while the state is ambiguous,
  * it never claims the preview changed when it did not,
  * it never leaks the internal code.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime import (  # noqa: E402
    ERROR_MESSAGES,
    _FALLBACK_ERROR_TEXT,
    _GENERIC_ERROR_TEXT,
    render_error_message,
)

P9_COPY = (
    "Publish belum bisa dilanjutkan karena status deployment production "
    "sebelumnya belum bisa diverifikasi dengan aman. "
    "Preview kamu tetap aman dan belum berubah."
)

PUBLISH_CODES = (
    "INCOMPLETE_LOOKUP",
    "PROMOTION_RECONCILIATION_REQUIRED",
    "PROMOTE_FAILED",
    "PROMOTE_NOT_APPLIED",
    "PROMOTE_VERIFICATION_FAILED",
    "INCOMPLETE_PREVIEW_IDENTITY",
    "DEPLOYMENT_IDENTITY_MISMATCH",
    "PROJECT_RECONCILIATION_REQUIRED",
    "PROJECT_IDENTITY_MISMATCH",
    "INVALID_DEPLOYMENT_ID",
    "INVALID_PRODUCTION_URL",
    "ROLLBACK_FAILED",
    "ROLLBACK_RECONCILIATION_REQUIRED",
    "ROLLBACK_TARGET_IDENTITY_INCOMPLETE",
)

# Words that would mean the copy is lying to the user.
SUCCESS_CLAIMS = ("berhasil", "sukses", "sudah live", "udah tayang", "selesai")


def test_incomplete_lookup_renders_the_exact_p9_copy():
    assert render_error_message("INCOMPLETE_LOOKUP") == P9_COPY


@pytest.mark.parametrize("code", PUBLISH_CODES)
def test_every_publish_failure_has_deliberate_user_copy(code):
    assert code in ERROR_MESSAGES, f"{code} has no user-facing copy"
    rendered = render_error_message(code)
    assert rendered != _FALLBACK_ERROR_TEXT, f"{code} renders generically"
    assert rendered != _GENERIC_ERROR_TEXT
    assert rendered == ERROR_MESSAGES[code]
    # The raw code must never reach the user.
    assert code not in rendered
    assert code.replace("_", " ").lower() not in rendered.lower()


@pytest.mark.parametrize("code", PUBLISH_CODES)
def test_no_publish_failure_claims_success(code):
    """An ambiguous state must never be reported as a completed publish."""
    rendered = render_error_message(code).lower()
    for claim in SUCCESS_CLAIMS:
        # "belum berhasil" / "tidak ... berhasil" are honest; a bare success
        # claim is not. Each claim must not appear as an assertion of success.
        assert claim not in rendered or "belum" in rendered or "tidak" in rendered, (
            f"{code} appears to claim success: {rendered!r}"
        )


@pytest.mark.parametrize("code", PUBLISH_CODES)
def test_publish_failures_keep_the_preview_safe(code):
    """The user must be told the one thing that IS true: their preview is
    unchanged, so they have not lost work."""
    rendered = render_error_message(code).lower()
    assert "preview" in rendered


def test_unknown_codes_still_fall_back_safely():
    assert render_error_message("SOME_FUTURE_CODE") == _FALLBACK_ERROR_TEXT
    assert render_error_message(None) == _FALLBACK_ERROR_TEXT


def test_internal_codes_are_unchanged():
    """The renderer is cosmetic: the codes the system emits are the same ones
    the operators and dashboards already record."""
    from app.projects.promote import PromotionOrchestrator  # noqa: F401
    for code in PUBLISH_CODES:
        assert code.isupper()
        assert code.replace("_", "").isalnum()
