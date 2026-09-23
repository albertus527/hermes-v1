"""Phase E — Vercel create-failure classification matrix (Bug 3).

Exercises the REAL ``VercelAdapter`` against a scripted transport. The old
behaviour classified EVERY non-2xx create response as
``AMBIGUOUS_PROJECT_CREATE``; these tests pin the evidence-based
classification.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.deploy.adapters import HttpResponse, VercelAdapter  # noqa: E402


class Transport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        status, body = value
        return HttpResponse(status, json.dumps(body).encode())


def _adapter(*responses):
    transport = Transport(*responses)
    return VercelAdapter("secret", "team_1", "installation", transport), transport


def _owned_project(a, name):
    return {"id": "prj_1", "name": name, "accountId": "team_1",
            "env": [{"key": "WEBSITE_BUILDER_OWNER", "value": a._marker("app"),
                     "type": "plain"}]}


def _foreign_project(name):
    return {"id": "prj_other", "name": name, "accountId": "team_1",
            "env": [{"key": "WEBSITE_BUILDER_OWNER", "value": "other", "type": "plain"}]}


def _post_counts(transport):
    return sum(1 for m, _, _ in transport.calls if m == "POST")


class TestEnsureProjectCreateClassification:
    def test_get_404_post_201_is_success(self):
        a, t = _adapter((404, {}), (201, {"id": "prj_1"}), (200, {}))
        t.responses[2] = (200, _owned_project(a, a.project_name_for("app")))
        result = a.ensure_project("app")
        assert result.success
        assert _post_counts(t) == 1

    def test_post_400_is_confirmed_failure_not_ambiguous(self):
        a, t = _adapter((404, {}), (400, {"error": "invalid name"}))
        result = a.ensure_project("app")
        assert not result.success
        assert result.error_code == "PROJECT_CREATE_REJECTED"
        assert _post_counts(t) == 1

    def test_post_409_owned_project_is_reconciled_not_ambiguous(self):
        # 409 => a project with that name exists and it is OURS.
        a, t = _adapter((404, {}), (409, {"error": "conflict"}), (200, {}))
        t.responses[2] = (200, _owned_project(a, a.project_name_for("app")))
        result = a.ensure_project("app")
        assert result.success
        assert _post_counts(t) == 1

    def test_post_409_foreign_project_is_proven_collision(self):
        a, t = _adapter((404, {}), (409, {"error": "conflict"}), (200, {}))
        t.responses[2] = (200, _foreign_project(a.project_name_for("app")))
        result = a.ensure_project("app")
        assert not result.success
        assert result.error_code == "PROJECT_NAME_TAKEN"
        assert _post_counts(t) == 1

    def test_post_timeout_is_ambiguous(self):
        a, t = _adapter((404, {}), TimeoutError("timed out"))
        result = a.ensure_project("app")
        assert not result.success
        assert result.error_code == "AMBIGUOUS_PROJECT_CREATE"
        assert _post_counts(t) == 1

    def test_post_500_is_ambiguous(self):
        a, t = _adapter((404, {}), (500, {"error": "boom"}))
        result = a.ensure_project("app")
        assert not result.success
        assert result.error_code == "AMBIGUOUS_PROJECT_CREATE"
        assert _post_counts(t) == 1


class TestEnsureProjectWithSlugCreateClassification:
    def test_slug_404_post_201_success(self):
        a, t = _adapter((404, {}), (201, {"id": "prj_1"}), (200, {}))
        t.responses[2] = (200, _owned_project(a, "dapur-kedaton"))
        result = a.ensure_project_with_slug("app", "dapur-kedaton")
        assert result.success
        assert _post_counts(t) == 1

    def test_slug_post_400_confirmed_failure(self):
        a, t = _adapter((404, {}), (400, {"error": "invalid"}))
        result = a.ensure_project_with_slug("app", "dapur-kedaton")
        assert not result.success
        assert result.error_code == "PROJECT_CREATE_REJECTED"
        assert _post_counts(t) == 1

    def test_slug_post_409_foreign_is_slug_collision(self):
        a, t = _adapter((404, {}), (409, {"error": "conflict"}),
                        (200, _foreign_project("dapur-kedaton")))
        result = a.ensure_project_with_slug("app", "dapur-kedaton")
        assert not result.success
        assert result.error_code == "SLUG_COLLISION"
        assert _post_counts(t) == 1

    def test_slug_post_409_owned_reconciles(self):
        a, t = _adapter((404, {}), (409, {"error": "conflict"}), (200, {}))
        t.responses[2] = (200, _owned_project(a, "dapur-kedaton"))
        result = a.ensure_project_with_slug("app", "dapur-kedaton")
        assert result.success
        assert _post_counts(t) == 1

    def test_slug_post_timeout_is_ambiguous_no_second_post(self):
        a, t = _adapter((404, {}), TimeoutError("timed out"))
        result = a.ensure_project_with_slug("app", "dapur-kedaton")
        assert not result.success
        assert result.error_code == "AMBIGUOUS_PROJECT_CREATE"
        assert _post_counts(t) == 1

    def test_slug_ambiguous_lookup_inconclusive_fails_closed(self):
        # 409 then a lookup that cannot prove ownership -> ambiguous, no retry.
        a, t = _adapter((404, {}), (409, {"error": "conflict"}), (500, {}))
        result = a.ensure_project_with_slug("app", "dapur-kedaton")
        assert not result.success
        assert result.error_code == "AMBIGUOUS_PROJECT_CREATE"
        assert _post_counts(t) == 1
