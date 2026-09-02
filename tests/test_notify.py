"""Phase 4 AC: correct links and buttons; nothing_eligible posts nothing."""

from __future__ import annotations

import json

import pytest

from ralph.contracts import AgentReport
from ralph.slack import (ACTION_APPROVE, ACTION_DISCARD, build_message,
                         should_notify)

# Slack Block Kit hard limits. Exceeding one is a send-time rejection, in an
# unattended run, with nobody watching -- so assert them here instead.
MAX_TEXT = 3000
MAX_BLOCK_ID = 255
MAX_BUTTON_TEXT = 75
MAX_BLOCKS = 50


def report(**overrides) -> AgentReport:
    payload = {
        "status": "in_review", "mode": "tagged", "ticket": "NIK-108",
        "branch": "ralph/NIK-108",
        "pr_url": "https://github.com/NikolayValev/quitting-smoking-tracker/pull/9",
        "preview_url": "https://preview.vercel.app",
        "summary": "Fixed 3 React Compiler violations; tests green.",
    }
    payload.update(overrides)
    return AgentReport.parse(payload, require_pr_url=False)


def blocks_of(rep, channel="#ralph"):
    return build_message(rep, channel=channel)["blocks"]


def block_types(rep):
    return [b["type"] for b in blocks_of(rep)]


# --- the silence rule -------------------------------------------------------

def test_nothing_eligible_is_silent():
    """AC: a quiet tick posts nothing. Pinging on every empty tick trains you
    to ignore the channel."""
    assert should_notify(report(status="nothing_eligible", pr_url="", preview_url="")) is False


@pytest.mark.parametrize("status", ["in_review", "blocked", "error"])
def test_other_statuses_do_notify(status):
    assert should_notify(report(status=status, pr_url="", preview_url="")) is True


# --- buttons ----------------------------------------------------------------

def test_in_review_gets_both_buttons():
    actions = [b for b in blocks_of(report()) if b["type"] == "actions"][0]
    assert [e["action_id"] for e in actions["elements"]] == [ACTION_APPROVE, ACTION_DISCARD]


def test_approve_button_links_to_the_pr():
    actions = [b for b in blocks_of(report()) if b["type"] == "actions"][0]
    approve = actions["elements"][0]
    assert approve["url"] == report().pr_url


def test_discard_button_requires_confirmation():
    """Discard deletes a branch; a mis-tap must not do that silently."""
    actions = [b for b in blocks_of(report()) if b["type"] == "actions"][0]
    discard = actions["elements"][1]
    assert "confirm" in discard
    assert "not merged" in discard["confirm"]["text"]["text"]


@pytest.mark.parametrize("status,pr", [("blocked", ""), ("error", "")])
def test_no_buttons_without_a_pr(status, pr):
    """Offering Approve on a run that produced no PR is meaningless."""
    assert "actions" not in block_types(report(status=status, pr_url=pr))


def test_in_review_without_pr_url_gets_no_buttons():
    assert "actions" not in block_types(report(pr_url=""))


# --- links ------------------------------------------------------------------

def test_context_carries_linear_pr_and_preview():
    context = [b for b in blocks_of(report()) if b["type"] == "context"][0]
    text = context["elements"][0]["text"]
    assert "linear.app/nikolayvalev/issue/NIK-108" in text
    assert "/pull/9" in text
    assert "preview.vercel.app" in text
    assert "ralph/NIK-108" in text


def test_missing_preview_is_stated_not_omitted():
    """A withheld preview must be visible as absent, not silently dropped."""
    context = [b for b in blocks_of(report(preview_url="")) if b["type"] == "context"][0]
    assert "_no preview_" in context["elements"][0]["text"]


# --- fallback text ----------------------------------------------------------

def test_fallback_text_is_present():
    """Without top-level `text` the push notification is blank."""
    message = build_message(report(), channel="#ralph")
    assert message["text"] and "NIK-108" in message["text"]


def test_links_do_not_unfurl():
    message = build_message(report(), channel="#ralph")
    assert message["unfurl_links"] is False and message["unfurl_media"] is False


# --- Slack's limits ---------------------------------------------------------

def test_respects_slack_limits_with_a_maximal_report():
    rep = report(summary="x" * 240, ticket="NIK-99999", branch="ralph/" + "b" * 60)
    message = build_message(rep, channel="#ralph")
    assert len(message["text"]) <= MAX_TEXT
    assert len(message["blocks"]) <= MAX_BLOCKS
    for block in message["blocks"]:
        if "block_id" in block:
            assert len(block["block_id"]) <= MAX_BLOCK_ID
        if block["type"] == "section":
            assert len(block["text"]["text"]) <= MAX_TEXT
        for element in block.get("elements", []):
            if element.get("type") == "button":
                assert len(element["text"]["text"]) <= MAX_BUTTON_TEXT
            if element.get("type") == "mrkdwn":
                assert len(element["text"]) <= MAX_TEXT


def test_payload_is_json_serialisable():
    """It goes over the wire as JSON; a stray non-serialisable value fails at send."""
    json.dumps(build_message(report(), channel="#ralph"))


def test_block_id_carries_the_ticket():
    """The listener resolves which ticket a button press refers to."""
    actions = [b for b in blocks_of(report()) if b["type"] == "actions"][0]
    assert actions["block_id"].endswith("::NIK-108")
