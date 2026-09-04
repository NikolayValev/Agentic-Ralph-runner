"""Phase 5 AC: buttons act; /ralph stop creates STOP; next tick no-ops."""

from __future__ import annotations

import json
from datetime import datetime

import pytest

import ralph.breaker as breaker
import ralph.commands as commands
import ralph.config as config_mod
from ralph.commands import handle_approve, handle_discard, handle_slash, status_text
from ralph.config import load
from slack_listener import dispatch_action, ticket_from_payload
from ralph.slack import ACTION_APPROVE, ACTION_DISCARD


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point STOP and the streak counter at a temp dir."""
    state = tmp_path / "state"
    state.mkdir()
    stop = state / "STOP"
    monkeypatch.setattr(config_mod, "STATE_DIR", state)
    monkeypatch.setattr(config_mod, "STOP_FILE", stop)
    monkeypatch.setattr(commands, "STOP_FILE", stop)
    monkeypatch.setattr(breaker, "STATE_DIR", state)
    monkeypatch.setattr(breaker, "STOP_FILE", stop)
    return stop


@pytest.fixture
def cfg():
    return load(check_env=False)


# --- /ralph stop and go -----------------------------------------------------

def test_stop_creates_the_stop_file(isolated, cfg):
    assert not isolated.exists()
    reply = handle_slash("stop", cfg)
    assert isolated.exists()
    assert "paused" in reply.text.lower()
    assert reply.ephemeral is False, "a pause is channel-visible, not private"


def test_go_clears_it(isolated, cfg):
    handle_slash("stop", cfg)
    handle_slash("go", cfg)
    assert not isolated.exists()


def test_go_when_not_stopped_is_harmless(isolated, cfg):
    reply = handle_slash("go", cfg)
    assert not isolated.exists() and "already running" in reply.text


def test_go_clears_the_failure_streak(isolated, cfg):
    """Otherwise resuming leaves the loop one failure from stopping again."""
    breaker.record("error", 3)
    breaker.record("error", 3)
    assert breaker.read_count() == 2
    handle_slash("go", cfg)
    assert breaker.read_count() == 0


def test_stop_is_idempotent(isolated, cfg):
    handle_slash("stop", cfg)
    handle_slash("stop", cfg)
    assert isolated.exists()


# --- status -----------------------------------------------------------------

def test_status_reports_paused(isolated, cfg):
    handle_slash("stop", cfg)
    assert "PAUSED" in status_text(cfg)


def test_status_reports_running(isolated, cfg):
    assert "running" in status_text(cfg)


def test_status_shows_window_membership(isolated, cfg):
    inside = status_text(cfg, now=datetime(2026, 9, 2, 3, 0))
    outside = status_text(cfg, now=datetime(2026, 9, 2, 17, 0))
    assert "inside" in inside and "outside" in outside


def test_status_shows_budget_and_streak(isolated, cfg):
    text = status_text(cfg)
    assert "run budget today:" in text and "failure streak:" in text


# --- parsing ----------------------------------------------------------------

@pytest.mark.parametrize("text", ["", "   ", "status", "STATUS", " s "])
def test_defaults_and_aliases_reach_status(isolated, cfg, text):
    assert "Ralph" in handle_slash(text, cfg).text


def test_unknown_command_shows_help(isolated, cfg):
    reply = handle_slash("frobnicate", cfg)
    assert "Unknown command" in reply.text and "/ralph status" in reply.text


def test_extra_arguments_are_ignored(isolated, cfg):
    """`/ralph stop now please` must still stop, not fall through to help."""
    handle_slash("stop now please", cfg)
    assert isolated.exists()


# --- buttons ----------------------------------------------------------------

def test_approve_does_not_merge(isolated, cfg):
    """The MVP stub. Merging stays a human action in GitHub (plan sec.11)."""
    reply = handle_approve(cfg, "NIK-108")
    assert "does not merge" in reply.text
    assert "NIK-108" in reply.text


def test_dispatch_routes_actions(isolated, cfg):
    assert "does not merge" in dispatch_action(cfg, ACTION_APPROVE, "NIK-108").text
    assert "Unknown action" in dispatch_action(cfg, "ralph_bogus", "NIK-108").text


def test_discard_reports_failure_rather_than_raising(isolated, cfg, monkeypatch):
    """A listener exception would kill the long-running process."""
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    reply = handle_discard(cfg, "NIK-108")
    assert "could not discard" in reply.text


# --- ticket resolution from a button press ----------------------------------

def test_ticket_from_button_value():
    payload = {"actions": [{"action_id": ACTION_DISCARD, "value": "NIK-108"}]}
    assert ticket_from_payload(payload) == "NIK-108"


def test_ticket_falls_back_to_block_id():
    payload = {"actions": [{"action_id": ACTION_DISCARD, "value": "",
                            "block_id": "ralph_actions::NIK-42"}]}
    assert ticket_from_payload(payload) == "NIK-42"


def test_ticket_missing_is_empty_not_an_error():
    assert ticket_from_payload({"actions": []}) == ""


# --- queue listing and prioritization ---------------------------------------

from ralph.commands import (handle_bump, handle_list, handle_skip,
                            handle_unskip, normalize_ticket)
from ralph.linear import URGENT, LinearError
from ralph.slack import ACTION_BUMP, ACTION_SKIP


def queue_issue(identifier, *, priority=0):
    return {
        "id": f"uuid-{identifier}", "identifier": identifier,
        "title": f"title for {identifier}", "created_at": "2026-01-01T00:00:00Z",
        "state_name": "Todo", "state_type": "unstarted",
        "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"],
        "priority": priority,
    }


@pytest.fixture
def fake_linear(monkeypatch):
    """Record writes instead of performing them."""
    calls = {"priority": [], "moves": []}
    monkeypatch.setattr(
        commands, "fetch_labelled_issues",
        lambda *a, **k: [queue_issue("NIK-1"), queue_issue("NIK-2", priority=URGENT)])
    monkeypatch.setattr(
        commands, "set_priority",
        lambda key, ticket, priority, **k: calls["priority"].append((ticket, priority)))
    monkeypatch.setattr(
        commands, "move_issue",
        lambda key, ticket, state, team, **k: (calls["moves"].append((ticket, state)), state)[1])
    return calls


@pytest.mark.parametrize("raw,expected", [
    ("NIK-110", "NIK-110"), ("nik-110", "NIK-110"), ("  NIK-110  ", "NIK-110"),
    ("", ""), ("garbage", ""), ("NIK-", ""), ("110", ""),
])
def test_normalize_ticket(raw, expected):
    assert normalize_ticket(raw) == expected


def test_normalize_ticket_accepts_matching_team():
    assert normalize_ticket("NIK-110", team_key="NIK") == "NIK-110"


def test_normalize_ticket_accepts_matching_team_case_insensitively():
    assert normalize_ticket("nik-110", team_key="NIK") == "NIK-110"


def test_normalize_ticket_rejects_a_foreign_team():
    assert normalize_ticket("ENG-99", team_key="NIK") == ""


def test_list_ranks_urgent_first_and_attaches_blocks(isolated, cfg, fake_linear):
    reply = handle_list(cfg)
    assert "NIK-2" in reply.text, "the urgent ticket is the next pick"
    assert reply.blocks, "the list is rendered as blocks, not just text"


def test_list_reports_a_linear_outage_without_raising(isolated, cfg, monkeypatch):
    def boom(*a, **k):
        raise LinearError("Linear request failed")
    monkeypatch.setattr(commands, "fetch_labelled_issues", boom)
    reply = handle_list(cfg)
    assert "could not read" in reply.text.lower()


@pytest.mark.parametrize("exc_cls", [ValueError, KeyError])
def test_list_reports_a_malformed_node_without_raising(isolated, cfg, monkeypatch, exc_cls):
    """A malformed Linear node raises ValueError/KeyError, not LinearError --
    catching only LinearError leaves the listener with no reply at all."""
    def boom(*a, **k):
        raise exc_cls("bad node")
    monkeypatch.setattr(commands, "fetch_labelled_issues", boom)
    reply = handle_list(cfg)
    assert "could not read" in reply.text.lower()


def test_bump_sets_urgent(isolated, cfg, fake_linear):
    reply = handle_bump(cfg, "NIK-1")
    assert fake_linear["priority"] == [("NIK-1", URGENT)]
    assert reply.ephemeral is False


def test_bump_rejects_a_malformed_ticket_without_writing(isolated, cfg, fake_linear):
    reply = handle_bump(cfg, "not-a-ticket")
    assert fake_linear["priority"] == []
    assert "usage" in reply.text.lower()


def test_bump_rejects_a_foreign_team_ticket_without_writing(isolated, cfg, fake_linear):
    """ENG-99 has the right shape but the wrong team; must not reach Linear."""
    reply = handle_bump(cfg, "ENG-99")
    assert fake_linear["priority"] == []
    assert "usage" in reply.text.lower()


def test_skip_rejects_a_foreign_team_ticket_without_writing(isolated, cfg, fake_linear):
    reply = handle_skip(cfg, "ENG-99")
    assert fake_linear["moves"] == []
    assert "usage" in reply.text.lower()


def test_unskip_rejects_a_foreign_team_ticket_without_writing(isolated, cfg, fake_linear):
    reply = handle_unskip(cfg, "ENG-99")
    assert fake_linear["moves"] == []
    assert "usage" in reply.text.lower()


def test_skip_parks_the_ticket_in_backlog(isolated, cfg, fake_linear):
    handle_skip(cfg, "NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["backlog_state"])]


# --- queue-membership check on bump/skip (not unskip) ------------------------
#
# fake_linear's fetch stub returns NIK-1 and NIK-2 (Urgent) as the queue.
# NIK-999 is shaped like a valid ticket but never appears in that queue, the
# same way a Done, non-existent, or in-flight (In Progress) ticket would not.

def test_bump_refuses_a_ticket_not_in_the_queue(isolated, cfg, fake_linear):
    reply = handle_bump(cfg, "NIK-999")
    assert fake_linear["priority"] == [], "no write on a ticket outside the queue"
    assert "not in the queue" in reply.text.lower()
    assert "NIK-1" in reply.text and "NIK-2" in reply.text, \
        "refusal should list the identifiers that ARE queued"


def test_skip_refuses_a_ticket_not_in_the_queue(isolated, cfg, fake_linear):
    reply = handle_skip(cfg, "NIK-999")
    assert fake_linear["moves"] == [], "no write on a ticket outside the queue"
    assert "not in the queue" in reply.text.lower()


def test_skip_refusal_points_at_stop_for_in_flight_work(isolated, cfg, fake_linear):
    """The human's actual recourse for a ticket already being worked is
    `/ralph stop`, not skip -- skip only affects tickets not yet started."""
    reply = handle_skip(cfg, "NIK-999")
    assert "/ralph stop" in reply.text


def test_bump_still_works_for_a_queued_ticket(isolated, cfg, fake_linear):
    reply = handle_bump(cfg, "NIK-1")
    assert fake_linear["priority"] == [("NIK-1", URGENT)]
    assert "goes next" in reply.text.lower()


def test_skip_still_works_for_a_queued_ticket(isolated, cfg, fake_linear):
    reply = handle_skip(cfg, "NIK-2")
    assert fake_linear["moves"] == [("NIK-2", cfg.linear["backlog_state"])]


def test_bump_reports_a_queue_outage_without_writing(isolated, cfg, fake_linear, monkeypatch):
    """The membership check itself can fail (Linear down); that must also
    produce no write, not a crash or a false 'it goes next'."""
    def boom(*a, **k):
        raise LinearError("Linear request failed")
    monkeypatch.setattr(commands, "fetch_labelled_issues", boom)
    reply = handle_bump(cfg, "NIK-1")
    assert fake_linear["priority"] == []
    assert "could not read" in reply.text.lower()


def test_unskip_is_not_subject_to_the_queue_check(isolated, cfg, fake_linear):
    """A parked ticket lives in Backlog and is by definition absent from the
    unstarted queue -- unskip must still work for it."""
    reply = handle_unskip(cfg, "NIK-999")
    assert fake_linear["moves"] == [("NIK-999", cfg.linear["todo_state"])]
    assert "not in the queue" not in reply.text.lower()


def test_unskip_returns_it_to_todo(isolated, cfg, fake_linear):
    handle_unskip(cfg, "NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["todo_state"])]


def test_slash_routes_the_new_commands(isolated, cfg, fake_linear):
    assert handle_slash("list", cfg).blocks
    handle_slash("bump NIK-1", cfg)
    assert fake_linear["priority"] == [("NIK-1", URGENT)]


def test_help_documents_the_new_commands(isolated, cfg):
    text = handle_slash("help", cfg).text
    for command in ("list", "bump", "skip", "unskip"):
        assert command in text


def test_dispatch_routes_bump(isolated, cfg, fake_linear):
    dispatch_action(cfg, ACTION_BUMP, "NIK-1")
    assert fake_linear["priority"] == [("NIK-1", URGENT)]


def test_dispatch_routes_skip(isolated, cfg, fake_linear):
    dispatch_action(cfg, ACTION_SKIP, "NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["backlog_state"])]


def test_dispatch_still_rejects_an_unknown_action(isolated, cfg, fake_linear):
    reply = dispatch_action(cfg, "ralph_nonsense", "NIK-1")
    assert "unknown" in reply.text.lower()
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []


# --- natural-language confirmation ------------------------------------------

from ralph.commands import handle_cancel, handle_confirm
from ralph.slack import ACTION_CANCEL, ACTION_CONFIRM, build_confirm_blocks


def test_confirm_blocks_carry_the_action_and_ticket():
    """The button value is the only state that survives the round trip."""
    blocks = build_confirm_blocks("bump", "NIK-115", "do the drizzle one next")
    actions = [b for b in blocks if b["type"] == "actions"][0]
    values = {e["action_id"]: e["value"] for e in actions["elements"]}
    assert values[ACTION_CONFIRM] == "bump::NIK-115"
    assert values[ACTION_CANCEL] == "bump::NIK-115"


def test_confirm_blocks_quote_what_was_understood():
    """The human is approving an interpretation, so it must be visible."""
    blocks = build_confirm_blocks("skip", "NIK-111", "park the haptics one")
    rendered = json.dumps(blocks)
    assert "NIK-111" in rendered and "skip" in rendered.lower()


def test_confirming_a_bump_performs_the_bump(isolated, cfg, fake_linear):
    reply = handle_confirm(cfg, "bump::NIK-1")
    assert fake_linear["priority"] == [("NIK-1", URGENT)]
    assert reply.ephemeral is False


def test_confirming_a_skip_performs_the_skip(isolated, cfg, fake_linear):
    handle_confirm(cfg, "skip::NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["backlog_state"])]


def test_cancelling_performs_nothing(isolated, cfg, fake_linear):
    reply = handle_cancel(cfg, "bump::NIK-1")
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []
    assert "cancel" in reply.text.lower()


def test_a_malformed_confirm_value_writes_nothing(isolated, cfg, fake_linear):
    """A stale or hand-crafted button payload must not reach Linear."""
    reply = handle_confirm(cfg, "garbage")
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []
    assert "could not" in reply.text.lower()


def test_an_unknown_action_in_a_confirm_writes_nothing(isolated, cfg, fake_linear):
    reply = handle_confirm(cfg, "deploy::NIK-1")
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []
    assert "could not" in reply.text.lower()


# --- confirming an action that has no ticket (go) ----------------------------

def test_confirm_blocks_for_a_ticketless_action_have_no_ticket_link():
    """`go` needs a click but names no ticket. Rendering linear_url("") would
    put a dead link to issue/ in front of the human."""
    blocks = build_confirm_blocks("go", "", "start it back up")
    rendered = json.dumps(blocks)
    assert "issue/|" not in rendered and "issue/>" not in rendered


def test_confirm_blocks_for_a_ticketless_action_still_carry_the_value():
    blocks = build_confirm_blocks("go", "", "start it back up")
    actions = [b for b in blocks if b["type"] == "actions"][0]
    assert actions["elements"][0]["value"] == "go::"


def test_confirming_go_resumes_the_loop(isolated, cfg, fake_linear):
    """The whole point of confirming `go`: it re-arms an autonomous system,
    so the click must actually clear STOP."""
    handle_slash("stop", cfg)
    assert isolated.exists()
    handle_confirm(cfg, "go::")
    assert not isolated.exists()
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []


def test_confirming_go_needs_no_ticket(isolated, cfg, fake_linear):
    reply = handle_confirm(cfg, "go::")
    assert "could not" not in reply.text.lower()


def test_stop_is_still_not_confirmable(isolated, cfg, fake_linear):
    """stop is deliberately immediate; it must not arrive through this path."""
    reply = handle_confirm(cfg, "stop::")
    assert "could not" in reply.text.lower()


# --- DM routing --------------------------------------------------------------

from slack_listener import handle_dm, is_human_dm


def test_a_bot_message_is_never_treated_as_a_dm():
    """The bot's own posts arrive as events too. Replying to them is an
    infinite loop that will rate-limit the workspace."""
    assert is_human_dm({"type": "message", "channel_type": "im",
                        "bot_id": "B123", "text": "hi"}) is False


def test_a_message_edit_is_ignored():
    """message_changed re-delivers old text and would re-run the action."""
    assert is_human_dm({"type": "message", "channel_type": "im",
                        "subtype": "message_changed", "text": "hi"}) is False


def test_a_channel_message_is_not_a_dm():
    assert is_human_dm({"type": "message", "channel_type": "channel",
                        "user": "U1", "text": "hi"}) is False


def test_a_non_message_event_is_ignored():
    assert is_human_dm({"type": "reaction_added", "channel_type": "im",
                        "user": "U1"}) is False


def test_a_plain_human_dm_is_accepted():
    assert is_human_dm({"type": "message", "channel_type": "im",
                        "user": "U1", "text": "status"}) is True


def test_a_dm_proposing_a_linear_write_asks_first(isolated, cfg, monkeypatch):
    import slack_listener
    from ralph.ollama import Intent
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (Intent("bump", "NIK-1", 0.9), ""))
    reply = handle_dm(cfg, "do the first one next")
    assert reply.blocks, "a write must be proposed, never performed"


def test_a_dm_proposing_go_asks_first(isolated, cfg, monkeypatch):
    """go re-arms the loop, so it is confirmed even though it writes no ticket."""
    import slack_listener
    from ralph.ollama import Intent
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (Intent("go", "", 0.9), ""))
    reply = handle_dm(cfg, "start it back up")
    assert reply.blocks


def test_a_dm_asking_to_stop_acts_immediately(isolated, cfg, monkeypatch):
    """stop is the kill switch: no second click between you and the brake."""
    import slack_listener
    from ralph.ollama import Intent
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (Intent("stop", "", 0.95), ""))
    reply = handle_dm(cfg, "stop for tonight")
    assert not reply.blocks and isolated.exists()


def test_a_dm_asking_for_status_answers_immediately(isolated, cfg, monkeypatch):
    import slack_listener
    from ralph.ollama import Intent
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (Intent("status", "", 0.95), ""))
    reply = handle_dm(cfg, "what are you doing")
    assert "Ralph" in reply.text and not reply.blocks


def test_a_dm_the_model_could_not_read_returns_the_question(isolated, cfg, monkeypatch):
    import slack_listener
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (None, "I did not follow that."))
    reply = handle_dm(cfg, "asdf")
    assert "did not follow" in reply.text and not reply.blocks


def test_conversation_can_be_switched_off_in_config():
    """A kill switch for the whole DM surface, independent of slash commands."""
    from ralph.config import load
    assert load(check_env=False).conversation.get("enabled") is True


def test_the_listener_gates_dms_on_both_the_switch_and_the_bot_check():
    """Static check: an events_api branch that forgets either guard is how the
    bot ends up answering itself or answering while disabled."""
    from pathlib import Path
    source = Path(__file__).resolve().parent.parent / "slack_listener.py"
    text = source.read_text(encoding="utf-8")
    events_at = text.index('"events_api"')
    tail = text[events_at:]
    assert "conversation" in tail, "the events branch must honour conversation.enabled"
    assert "is_human_dm" in tail, "the events branch must reject bot and edited messages"
    assert tail.index("is_human_dm") < tail.index("handle_dm"), (
        "the bot check must gate the handler, not follow it")
