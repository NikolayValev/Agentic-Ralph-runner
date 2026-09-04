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


# --- the start-of-run message -----------------------------------------------

def _running_report():
    return AgentReport(
        status="running", mode="tagged", ticket="NIK-105",
        branch="ralph/NIK-105", pr_url="", preview_url="",
        summary="agent running",
    )


def test_running_message_is_visually_distinct():
    """A live run must not look like a finished one at a glance."""
    payload = build_message(_running_report(), channel="#ralph")
    assert ":hourglass_flowing_sand:" in payload["text"]
    assert "NIK-105" in payload["text"]


def test_running_message_offers_no_review_buttons():
    """There is nothing to approve or discard until the run finishes."""
    payload = build_message(_running_report(), channel="#ralph")
    assert not [b for b in payload["blocks"] if b["type"] == "actions"]


def test_running_message_does_not_claim_a_missing_preview():
    """'no preview' is true but useless before the run has pushed anything;
    it reads as a failure rather than as work still in progress."""
    payload = build_message(_running_report(), channel="#ralph")
    rendered = json.dumps(payload)
    assert "no preview" not in rendered


def test_a_running_report_is_not_silent():
    """The whole point is to hear that a tick started."""
    assert should_notify(_running_report()) is True


# --- message handles: editing the start message into the final one ----------

def test_handle_round_trips(tmp_path):
    """chat.update needs the resolved channel ID, not '#ralph' -- storing only
    the ts would fail at edit time."""
    import notify
    path = tmp_path / "handle.json"
    notify.write_handle(str(path), "C0123ABC", "1788.0001")
    assert notify.read_handle(str(path)) == ("C0123ABC", "1788.0001")


def test_read_handle_returns_none_when_absent(tmp_path):
    import notify
    assert notify.read_handle(str(tmp_path / "missing.json")) is None


def test_deliver_edits_the_existing_message_when_a_handle_exists(tmp_path, monkeypatch):
    import notify
    path = tmp_path / "handle.json"
    notify.write_handle(str(path), "C0123ABC", "1788.0001")
    calls = []
    monkeypatch.setattr(notify, "update", lambda p, t, c, ts: calls.append(("update", c, ts)) or ts)
    monkeypatch.setattr(notify, "post", lambda p, t: calls.append(("post",)) or ("C", "9"))

    notify.deliver({"channel": "#ralph"}, "token", handle_path=str(path))
    assert calls == [("update", "C0123ABC", "1788.0001")]


def test_deliver_falls_back_to_posting_when_the_edit_fails(tmp_path, monkeypatch):
    """A duplicate message is a far better failure than a silently missing result."""
    import notify
    path = tmp_path / "handle.json"
    notify.write_handle(str(path), "C0123ABC", "1788.0001")
    calls = []

    def boom(*a, **k):
        raise notify.NotifyError("message_not_found")

    monkeypatch.setattr(notify, "update", boom)
    monkeypatch.setattr(notify, "post", lambda p, t: calls.append("post") or ("C0123ABC", "1788.9"))

    notify.deliver({"channel": "#ralph"}, "token", handle_path=str(path))
    assert calls == ["post"]


def test_deliver_posts_when_there_is_no_handle(tmp_path, monkeypatch):
    import notify
    calls = []
    monkeypatch.setattr(notify, "post", lambda p, t: calls.append("post") or ("C", "1"))
    monkeypatch.setattr(notify, "update", lambda *a: calls.append("update"))

    notify.deliver({"channel": "#ralph"}, "token", handle_path=str(tmp_path / "none.json"))
    assert calls == ["post"]


def _report_file(tmp_path, status="running", pr_url=""):
    path = tmp_path / "report.json"
    path.write_text(json.dumps({
        "status": status, "mode": "tagged", "ticket": "NIK-105",
        "branch": "ralph/NIK-105", "pr_url": pr_url, "preview_url": "",
        "summary": "agent running",
    }), encoding="utf-8")
    return str(path)


def test_main_emits_a_handle_after_posting(tmp_path, monkeypatch):
    """Without this the start message cannot be found again to edit."""
    import notify
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#ralph")
    monkeypatch.setattr(notify, "post", lambda p, t: ("C0123ABC", "1788.0001"))

    handle = tmp_path / "handle.json"
    rc = notify.main(["--report-file", _report_file(tmp_path),
                      "--emit-handle", str(handle)])
    assert rc == 0
    assert notify.read_handle(str(handle)) == ("C0123ABC", "1788.0001")


def test_main_updates_through_the_handle(tmp_path, monkeypatch):
    import notify
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNEL", "#ralph")
    handle = tmp_path / "handle.json"
    notify.write_handle(str(handle), "C0123ABC", "1788.0001")

    edited = []
    monkeypatch.setattr(notify, "update",
                        lambda p, t, c, ts: edited.append((c, ts)) or ts)
    monkeypatch.setattr(notify, "post",
                        lambda p, t: (_ for _ in ()).throw(AssertionError("should have edited")))

    rc = notify.main(["--report-file", _report_file(tmp_path, "in_review", "https://x/1"),
                      "--update-handle", str(handle)])
    assert rc == 0 and edited == [("C0123ABC", "1788.0001")]
