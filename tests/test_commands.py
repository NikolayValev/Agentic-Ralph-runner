"""Phase 5 AC: buttons act; /ralph stop creates STOP; next tick no-ops."""

from __future__ import annotations

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
