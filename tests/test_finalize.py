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


# --- Vercel ids come from the environment, not the tracked config -----------

def test_deploy_id_prefers_the_environment(monkeypatch):
    """config.yaml is public; .env is not. The env must win."""
    from finalize import deploy_id
    from ralph.config import load

    cfg = load(check_env=False)
    monkeypatch.setenv("VERCEL_PROJECT_ID", "prj_from_env")
    assert deploy_id(cfg, "VERCEL_PROJECT_ID", "vercel_project_id") == "prj_from_env"


def test_deploy_id_falls_back_to_config(monkeypatch):
    """A private deployment of this repo may still keep the ids in config."""
    from finalize import deploy_id
    from ralph.config import load

    cfg = load(check_env=False)
    cfg.raw["deploy"]["vercel_project_id"] = "prj_from_config"
    monkeypatch.delenv("VERCEL_PROJECT_ID", raising=False)
    assert deploy_id(cfg, "VERCEL_PROJECT_ID", "vercel_project_id") == "prj_from_config"


def test_tracked_config_carries_no_account_identifiers():
    """Regression guard for the public repo: these belong in .env only."""
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    for name in ("config.yaml", ".env.example"):
        text = (root / name).read_text(encoding="utf-8")
        found = re.findall(r"\b(?:prj_|team_)(?!xxx)[A-Za-z0-9]{8,}", text)
        assert not found, f"{name} carries a real Vercel identifier: {found}"
