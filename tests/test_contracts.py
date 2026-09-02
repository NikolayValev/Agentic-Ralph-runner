"""Phase 0 AC: the three SS7 contracts validate strictly at each seam."""

from __future__ import annotations

import json

import pytest

from ralph.contracts import (
    AgentReport,
    ContractError,
    PreFilter,
    Task,
    parse_report_from_output,
)

VALID_REPORT = {
    "status": "in_review",
    "mode": "tagged",
    "ticket": "NIK-111",
    "branch": "ralph/NIK-111",
    "pr_url": "https://github.com/NikolayValev/quitting-smoking-tracker/pull/7",
    "preview_url": "https://qst-abc123.vercel.app",
    "summary": "Added PostHog capture calls to the three primary actions.",
}


# --- Task -------------------------------------------------------------------

def test_task_roundtrip():
    task = Task.parse({"mode": "tagged", "ref": "NIK-111"})
    assert task.ref == "NIK-111"
    assert json.loads(task.to_json()) == {"mode": "tagged", "ref": "NIK-111"}


def test_task_rejects_scout_mode():
    """Scout is parked for MVP; a scout task must not silently pass the seam."""
    with pytest.raises(ContractError, match="must be one of"):
        Task.parse({"mode": "scout", "ref": "NIK-111"})


@pytest.mark.parametrize("payload", [
    {"mode": "tagged"},                  # missing ref
    {"ref": "NIK-111"},                  # missing mode
    {"mode": "tagged", "ref": ""},       # empty ref
    {"mode": "tagged", "ref": "   "},    # whitespace ref
    {"mode": "tagged", "ref": 111},      # wrong type
])
def test_task_rejects_malformed(payload):
    with pytest.raises(ContractError):
        Task.parse(payload)


# --- PreFilter --------------------------------------------------------------

def test_prefilter_roundtrip():
    pf = PreFilter.parse({
        "run": True, "tier": "claude", "reason": "real code change", "commit_hint": "feat: x",
    })
    assert pf.run is True and pf.tier == "claude"


def test_prefilter_rejects_unknown_tier():
    with pytest.raises(ContractError, match="tier"):
        PreFilter.parse({
            "run": True, "tier": "opus", "reason": "r", "commit_hint": "c",
        })


def test_prefilter_rejects_truthy_nonbool_run():
    """A local model emitting "true" as a string must not be read as a go."""
    with pytest.raises(ContractError, match="'run' must be bool"):
        PreFilter.parse({
            "run": "true", "tier": "claude", "reason": "r", "commit_hint": "c",
        })


# --- AgentReport ------------------------------------------------------------

def test_report_roundtrip():
    report = AgentReport.parse(VALID_REPORT)
    assert report.ticket == "NIK-111"
    assert json.loads(report.to_json())["status"] == "in_review"


def test_report_rejects_unknown_status():
    with pytest.raises(ContractError, match="status"):
        AgentReport.parse(VALID_REPORT | {"status": "merged"})


def test_report_rejects_oversized_summary():
    with pytest.raises(ContractError, match="<= 240"):
        AgentReport.parse(VALID_REPORT | {"summary": "x" * 241})


def test_report_summary_at_limit_is_ok():
    assert len(AgentReport.parse(VALID_REPORT | {"summary": "x" * 240}).summary) == 240


def test_final_report_in_review_requires_pr_url():
    """The whole point of a run is a reviewable PR; claiming one without a URL is a bug."""
    with pytest.raises(ContractError, match="requires a non-empty 'pr_url'"):
        AgentReport.parse(VALID_REPORT | {"pr_url": ""})


def test_agent_stage_may_omit_pr_url():
    """The agent has no capability to open a PR -- the wrapper fills this in."""
    report = AgentReport.parse(VALID_REPORT | {"pr_url": ""}, require_pr_url=False)
    assert report.status == "in_review" and report.pr_url == ""


def test_blocked_status_may_omit_pr_url():
    report = AgentReport.parse(VALID_REPORT | {"status": "blocked", "pr_url": ""})
    assert report.status == "blocked"


# --- extraction from agent stdout -------------------------------------------

def test_extracts_report_from_trailing_prose():
    out = (
        "Reading the ticket...\n"
        "Ran tests, 42 passed.\n"
        + json.dumps(VALID_REPORT) + "\n"
        "Done.\n"
    )
    assert parse_report_from_output(out).ticket == "NIK-111"


def test_extracts_from_fenced_json():
    out = "blah\n```json\n" + json.dumps(VALID_REPORT) + "\n```\n"
    assert parse_report_from_output(out).status == "in_review"


def test_last_report_wins():
    """An early draft object must not shadow the agent's final answer."""
    early = json.dumps(VALID_REPORT | {"status": "blocked", "pr_url": ""})
    out = early + "\n...more work...\n" + json.dumps(VALID_REPORT)
    assert parse_report_from_output(out).status == "in_review"


def test_no_report_raises():
    with pytest.raises(ContractError, match="no agent report"):
        parse_report_from_output("I could not complete the task.")


def test_non_report_json_is_skipped():
    out = '{"some": "other object"}\n' + json.dumps(VALID_REPORT)
    assert parse_report_from_output(out).ticket == "NIK-111"
