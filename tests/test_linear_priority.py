"""The one new Linear write. Validation happens before the request, not after."""

from __future__ import annotations

import pytest

import ralph.linear as linear
from ralph.linear import URGENT, LinearError, set_priority


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if a test reaches _post when it should not have."""
    def explode(*args, **kwargs):
        raise AssertionError("_post was called; validation should have stopped it")
    monkeypatch.setattr(linear, "_post", explode)


@pytest.mark.parametrize("bad", [-1, 5, 99, "urgent", None])
def test_invalid_priority_is_rejected_without_a_request(no_network, bad):
    with pytest.raises(LinearError, match="priority"):
        set_priority("key", "NIK-1", bad)


def test_valid_priority_is_sent_and_confirmed(monkeypatch):
    seen = {}

    def fake_post(api_key, query, variables, endpoint=linear.ENDPOINT):
        seen["variables"] = variables
        return {"issueUpdate": {"success": True,
                                "issue": {"identifier": "NIK-1", "priority": URGENT}}}

    monkeypatch.setattr(linear, "_post", fake_post)
    assert set_priority("key", "NIK-1", URGENT) == URGENT
    assert seen["variables"] == {"id": "NIK-1", "priority": URGENT}


def test_a_refused_update_raises(monkeypatch):
    monkeypatch.setattr(
        linear, "_post",
        lambda *a, **k: {"issueUpdate": {"success": False, "issue": None}})
    with pytest.raises(LinearError, match="refused"):
        set_priority("key", "NIK-1", URGENT)
