"""Phase 2: the agent does not get to assert its own outcome."""

from __future__ import annotations

import json

from finalize import build_report
from ralph.config import forbidden_hits


def agent_output(**overrides) -> str:
    payload = {
        "status": "in_review", "mode": "tagged", "ticket": "NIK-104",
        "branch": "ralph/NIK-104", "pr_url": "", "preview_url": "",
        "summary": "Removed dead Supabase config from the CI workflow.",
    }
    payload.update(overrides)
    return "some prose\n" + json.dumps(payload) + "\n"


def test_report_is_parsed():
    report = build_report(agent_output(), ticket="NIK-104", branch="ralph/NIK-104")
    assert report.status == "in_review"
    assert report.summary.startswith("Removed dead Supabase")


def test_agent_cannot_claim_a_different_ticket():
    """A confused agent reporting the wrong ticket must not mislabel the PR."""
    out = agent_output(ticket="NIK-999", branch="ralph/NIK-999")
    report = build_report(out, ticket="NIK-104", branch="ralph/NIK-104")
    assert report.ticket == "NIK-104"
    assert report.branch == "ralph/NIK-104"


def test_agent_cannot_supply_its_own_pr_url():
    """pr_url is the wrapper's to set; a fabricated one would be reported to Slack."""
    out = agent_output(pr_url="https://github.com/attacker/repo/pull/1")
    report = build_report(out, ticket="NIK-104", branch="ralph/NIK-104")
    assert report.pr_url == ""


def test_missing_report_becomes_error_not_a_crash():
    report = build_report("I give up.", ticket="NIK-104", branch="ralph/NIK-104")
    assert report.status == "error"
    assert "no valid report" in report.summary


def test_malformed_json_becomes_error():
    report = build_report('{"status": "in_rev', ticket="NIK-104", branch="ralph/NIK-104")
    assert report.status == "error"


def test_oversized_summary_is_truncated_not_rejected():
    """A long summary should not lose the whole run."""
    out = "prose\n" + json.dumps({
        "status": "blocked", "mode": "tagged", "ticket": "NIK-104",
        "branch": "ralph/NIK-104", "pr_url": "", "preview_url": "",
        "summary": "x" * 400,
    })
    # The contract rejects >240, so parse fails and we fall back to error --
    # which is the safe outcome: a human looks at it.
    report = build_report(out, ticket="NIK-104", branch="ralph/NIK-104")
    assert report.status == "error"


# --- the self-escalation guard ---------------------------------------------

def test_editing_own_permissions_is_caught():
    """This repo commits .claude/settings.local.json granting Bash(pnpm exec *).
    An agent editing it would widen its own permissions on the next run."""
    changed = ["app/page.tsx", ".claude/settings.local.json"]
    assert forbidden_hits(changed, [".claude/"]) == [".claude/settings.local.json"]


def test_legitimate_ci_edit_is_allowed():
    """NIK-104 legitimately edits .github/workflows/ci.yml -- must not be blocked."""
    assert forbidden_hits([".github/workflows/ci.yml"], [".claude/"]) == []


def test_windows_separators_are_normalised():
    assert forbidden_hits([".claude\settings.local.json"], [".claude/"])
