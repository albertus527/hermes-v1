"""BUG 2 regression: the protected preview URL is internal state, and the
user-facing preview is screenshots only.

The p9 E2E sent the Vercel preview deployment URL to the user. That
deployment sits behind Vercel Deployment Protection, so the link is
unopenable for the person who received it, and "fixing" it by publishing the
deployment would dismantle the very protection the mandatory smoke check
depends on. The URL therefore stays in durable state (the approval/promotion
binding and the smoke test both need it) and out of every outbound message.

These tests pin:

  * preview_url is still persisted and still bound to the shown preview,
  * no outbound photo caption / text / follow-up contains the URL or its host,
  * BOTH screenshots (desktop, then mobile) are delivered as separate,
    individually-tracked messages,
  * a preview is only marked shown once EVERY delivery is confirmed sent,
  * each screenshot keeps at-most-once semantics across a crash.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.contracts import OperationResult  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.git_output import OutputGitRepository  # noqa: E402
from app.deploy.preview import (  # noqa: E402
    _DELIVERY_PENDING,
    _DELIVERY_SENT,
    PreviewDeps,
    PreviewOrchestrator,
)

from test_preview_orchestrator import (  # noqa: E402
    FakeSmoke,
    FakeTelegram,
    FakeVercel,
    _make_workspace,
    _preview_ready_state,
)

PREVIEW_HOST = "tested.vercel.app"
PREVIEW_URL = f"https://{PREVIEW_HOST}"
DISPLAY_NAME = "webbandung"


class RecordingTelegram(FakeTelegram):
    """Records the caption of every photo so caption leakage is observable."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.photo_calls = []
        self.text_calls = []

    def send_photo(self, chat_id, path, caption=''):
        self.photo_calls.append({'chat_id': str(chat_id), 'path': str(path),
                                 'caption': caption})
        return OperationResult.ok({'message_id': len(self.photo_calls)})

    def send_text(self, chat_id, text):
        self.text_calls.append({'chat_id': str(chat_id), 'text': text})
        return OperationResult.ok({'message_id': 100 + len(self.text_calls)})


def _deps(tmp_path, telegram, smoke=None):
    return PreviewDeps(
        vercel=FakeVercel(),
        telegram=telegram,
        smoke=smoke or FakeSmoke(),
        output_repo=OutputGitRepository(tmp_path / 'out',
                                       hermes_root=tmp_path / 'hermes'),
        chat_id_for=lambda pid, state: '123',
        display_name_for=lambda pid, state: DISPLAY_NAME,
    )


def _orchestrator(tmp_path, telegram=None, smoke=None):
    store = ProjectStateStore(Path(tmp_path) / 'state')
    ws = _make_workspace(tmp_path)
    _preview_ready_state(store, 'proj', ws)
    telegram = telegram or RecordingTelegram()
    return PreviewOrchestrator(store, _deps(tmp_path, telegram, smoke)), \
        store, ws, telegram


def _outbound_text(telegram):
    return [m['text'] for m in telegram.text_calls] + \
           [m['caption'] for m in telegram.photo_calls]


# ---------------------------------------------------------------------------
# The URL is internal, the screenshots are the deliverable
# ---------------------------------------------------------------------------

def test_preview_url_is_persisted_but_never_sent(tmp_path):
    orch, store, ws, telegram = _orchestrator(tmp_path)

    result = orch.run_owned('proj', ws)

    assert result.success, result.error
    state = store.load('proj')

    # Persisted: the identity binding needs it.
    intent = state.deployment['preview_intent']
    shown = state.deployment['latest_shown_preview']
    assert intent['preview_url'] == PREVIEW_URL
    assert shown['preview_url'] == PREVIEW_URL
    # ...and the result the caller sees.
    assert result.data['preview_url'] == PREVIEW_URL

    # Never sent: nothing outbound may contain the URL or even its host.
    outbound = _outbound_text(telegram)
    assert outbound, "the preview must still deliver something to the user"
    for message in outbound:
        assert PREVIEW_URL not in message
        assert "vercel.app" not in message
        # No deployment id / operation id leaks either.
        assert shown['deployment_id'] not in message
        assert shown['operation_id'] not in message


def test_both_screenshots_are_delivered_in_fixed_order(tmp_path):
    orch, store, ws, telegram = _orchestrator(tmp_path)

    result = orch.run_owned('proj', ws)

    assert result.success, result.error
    assert [m['caption'] for m in telegram.photo_calls] == [
        "Preview sudah siap — tampilan desktop.",
        "Preview sudah siap — tampilan mobile.",
    ]
    # The screenshots themselves are real PNGs on disk.
    for call in telegram.photo_calls:
        assert Path(call['path']).read_bytes().startswith(b'\x89PNG\r\n\x1a\n')
    intent = store.load('proj').deployment['preview_intent']
    assert intent['screenshot_outcome'] == {
        'desktop_screenshot': 'SENT', 'mobile_screenshot': 'SENT',
    }
    assert intent['screenshot_attempted'] == [
        'desktop_screenshot', 'mobile_screenshot',
    ]


def test_instruction_text_asks_for_a_revision_or_approval(tmp_path):
    orch, store, ws, telegram = _orchestrator(tmp_path)

    assert orch.run_owned('proj', ws).success

    texts = [m['text'] for m in telegram.text_calls]
    assert any('Preview sudah siap' in t for t in texts)
    assert any('approve' in t for t in texts)
    assert any('revisi' in t for t in texts)
    # Exactly the delivery text plus the natural follow-up.
    assert len(texts) == 2


def test_follow_up_never_carries_the_url(tmp_path):
    orch, store, ws, telegram = _orchestrator(tmp_path)

    assert orch.run_owned('proj', ws).success

    follow = [m['text'] for m in telegram.text_calls][-1]
    assert 'Mau revisi' in follow
    assert PREVIEW_URL not in follow and "vercel.app" not in follow


# ---------------------------------------------------------------------------
# All deliveries before "latest shown preview"
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('failed_key', ('desktop_screenshot', 'mobile_screenshot'))
def test_preview_is_not_shown_while_a_screenshot_is_ambiguous(tmp_path, failed_key):
    """A screenshot whose send outcome is unknown blocks the shown-preview
    write entirely -- the user may have seen it, so nothing may claim it was
    delivered, and a re-drive must not resend it."""
    orch, store, ws, telegram = _orchestrator(tmp_path)
    assert orch.run_owned('proj', ws).success

    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        outcomes = dict(state.deployment['preview_intent']['screenshot_outcome'])
        outcomes[failed_key] = _DELIVERY_PENDING
        state.deployment['preview_intent']['screenshot_outcome'] = outcomes
        state.deployment['preview_intent']['photo_outcome'] = _DELIVERY_PENDING
        store.save(state)
    telegram.photo_calls.clear()
    telegram.text_calls.clear()

    again = orch.run_owned('proj', ws)

    assert not again.success
    assert again.error_code == 'DELIVERY_RECONCILIATION_REQUIRED'
    assert telegram.photo_calls == [] and telegram.text_calls == []
    assert store.load('proj').deployment.get('latest_shown_preview') is None


def test_re_drive_sends_only_the_unconfirmed_screenshot(tmp_path):
    """Desktop confirmed, mobile provably not sent: exactly the mobile is
    re-driven -- no duplicate desktop, no repeated text."""
    orch, store, ws, telegram = _orchestrator(tmp_path)
    assert orch.run_owned('proj', ws).success

    with store.acquire_writer('proj') as state:
        state.deployment.pop('latest_shown_preview', None)
        state.revisions.preview_revision = 0
        intent = state.deployment['preview_intent']
        intent['screenshot_outcome'] = {
            'desktop_screenshot': 'SENT', 'mobile_screenshot': 'NOT_SENT',
        }
        intent['photo_outcome'] = 'NOT_SENT'
        store.save(state)
    telegram.photo_calls.clear()
    telegram.text_calls.clear()

    again = orch.run_owned('proj', ws)

    assert again.success, again.error
    assert [m['caption'] for m in telegram.photo_calls] == [
        "Preview sudah siap — tampilan mobile.",
    ]
    # The preview instruction text was already confirmed sent, so it is not
    # repeated. (The follow-up is keyed on the shown-preview row, which this
    # re-drive rewrote, so it fires for the newly shown preview.)
    assert not any('Preview sudah siap' in m['text'] for m in telegram.text_calls)
    assert store.load('proj').revisions.preview_revision == 1


def test_shown_preview_gate_requires_every_delivery(tmp_path):
    """The gate reads the DURABLE outcomes, not the in-memory loop state."""
    orch = PreviewOrchestrator.__new__(PreviewOrchestrator)
    # A re-drive that skipped both sends (already SENT) must still pass the
    # gate on durable evidence.
    prior = {
        'screenshot_outcome': {
            'desktop_screenshot': 'SENT', 'mobile_screenshot': 'SENT',
        },
        'text_outcome': 'SENT',
    }
    assert orch._photo_aggregate(prior['screenshot_outcome']) == 'SENT'

    partial = {'screenshot_outcome': {'desktop_screenshot': 'SENT'}}
    assert orch._photo_aggregate(partial['screenshot_outcome']) == 'PENDING'
    assert orch._screenshot_outcomes(
        {'screenshot_outcome': partial['screenshot_outcome']}
    ) == {'desktop_screenshot': 'SENT'}


def test_legacy_single_photo_row_still_reads_faithfully(tmp_path):
    """A row written before per-screenshot outcomes existed degrades correctly:
    its single photo outcome maps onto the desktop shot, and the mobile shot is
    simply not-yet-attempted (safe to deliver, never a duplicate)."""
    orch = PreviewOrchestrator.__new__(PreviewOrchestrator)

    legacy_sent = orch._screenshot_outcomes(
        {'photo_attempted': True, 'photo_outcome': 'SENT'}
    )
    assert legacy_sent == {'desktop_screenshot': 'SENT'}

    legacy_unknown = orch._screenshot_outcomes({'photo_attempted': True})
    assert legacy_unknown == {'desktop_screenshot': _DELIVERY_PENDING}

    legacy_untouched = orch._screenshot_outcomes({})
    assert legacy_untouched == {}


def test_smoke_failure_delivers_nothing_at_all(tmp_path):
    orch, store, ws, telegram = _orchestrator(
        tmp_path, smoke=FakeSmoke(success=False),
    )

    result = orch.run_owned('proj', ws)

    assert not result.success
    assert telegram.photo_calls == [] and telegram.text_calls == []
    assert store.load('proj').deployment.get('latest_shown_preview') is None
