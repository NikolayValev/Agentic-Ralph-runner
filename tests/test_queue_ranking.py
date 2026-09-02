"""Ranking: Linear priority first, then oldest-first.

The trap this file exists to guard: Linear encodes 0 as "No priority" and 1 as
"Urgent", so sorting on the raw value puts unprioritized tickets ahead of
urgent ones -- the exact opposite of what a human means by "prioritized".
"""

from __future__ import annotations

from ralph.linear import (NO_PRIORITY_RANK, URGENT, priority_rank, rank_issues,
                          select_ticket)

ELIGIBLE = "autonomous-eligible"
REPO = "repo:quitting-smoking-tracker"


def issue(identifier, *, priority=0, created_at="2026-01-01T00:00:00Z",
          labels=None, state_type="unstarted"):
    """A normalized issue, in the shape _normalize produces."""
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"title for {identifier}",
        "created_at": created_at,
        "state_name": "Todo",
        "state_type": state_type,
        "labels": [ELIGIBLE, REPO] if labels is None else labels,
        "priority": priority,
    }


def order(issues):
    ranked, _ = rank_issues(issues, eligible_label=ELIGIBLE, repo_label=REPO)
    return [i["identifier"] for i in ranked]


def test_no_priority_ranks_below_low():
    assert priority_rank({"priority": 0}) == NO_PRIORITY_RANK
    assert priority_rank({"priority": 4}) == 4
    assert priority_rank({"priority": URGENT}) == 1


def test_missing_priority_key_is_treated_as_no_priority():
    """Issues from a fixture file or an older cache may not carry the field."""
    assert priority_rank({}) == NO_PRIORITY_RANK
    assert priority_rank({"priority": None}) == NO_PRIORITY_RANK


def test_unprioritized_sorts_last_not_first():
    assert order([issue("NIK-1", priority=0), issue("NIK-2", priority=4)]) == ["NIK-2", "NIK-1"]


def test_urgent_beats_older_unprioritized():
    """The whole point of the feature: a bump wins over age."""
    old = issue("NIK-1", priority=0, created_at="2020-01-01T00:00:00Z")
    bumped = issue("NIK-2", priority=URGENT, created_at="2026-06-01T00:00:00Z")
    assert order([old, bumped]) == ["NIK-2", "NIK-1"]


def test_equal_priority_falls_back_to_oldest_first():
    newer = issue("NIK-1", priority=2, created_at="2026-06-01T00:00:00Z")
    older = issue("NIK-2", priority=2, created_at="2026-01-01T00:00:00Z")
    assert order([newer, older]) == ["NIK-2", "NIK-1"]


def test_identical_priority_and_age_falls_back_to_identifier():
    """Determinism across runs matters: the gate must not flap."""
    assert order([issue("NIK-9"), issue("NIK-2")]) == ["NIK-2", "NIK-9"]


def test_ineligible_issues_are_excluded_with_reasons():
    ranked, skipped = rank_issues(
        [issue("NIK-1"), issue("NIK-2", state_type="backlog")],
        eligible_label=ELIGIBLE, repo_label=REPO)
    assert [i["identifier"] for i in ranked] == ["NIK-1"]
    assert len(skipped) == 1 and "NIK-2" in skipped[0]


def test_select_ticket_returns_the_head_of_the_ranking():
    head, skipped = select_ticket(
        [issue("NIK-1", priority=0), issue("NIK-2", priority=URGENT)],
        eligible_label=ELIGIBLE, repo_label=REPO)
    assert head["identifier"] == "NIK-2"
    assert skipped == []


def test_select_ticket_returns_none_when_nothing_is_eligible():
    head, skipped = select_ticket(
        [issue("NIK-1", state_type="backlog")],
        eligible_label=ELIGIBLE, repo_label=REPO)
    assert head is None and len(skipped) == 1
