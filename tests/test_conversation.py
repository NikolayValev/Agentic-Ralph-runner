"""Turning a sentence into an intent, and refusing to guess.

The model never reaches Linear here: interpret() only decides WHAT to
propose. Executing it is the confirmation step's job.
"""

from __future__ import annotations

import pytest

import ralph.conversation as conversation
from ralph.config import load
from ralph.ollama import Intent, OllamaError


@pytest.fixture
def cfg():
    return load(check_env=False)


@pytest.fixture
def queue(monkeypatch):
    """Two tickets, so 'the drizzle one' has something to match against."""
    issues = [
        {"identifier": "NIK-111", "title": "Add haptics to breathing timer",
         "priority": 0, "created_at": "2026-01-01T00:00:00Z", "state_type": "unstarted",
         "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"], "id": "u1"},
        {"identifier": "NIK-115", "title": "Migrate to Drizzle 0.31",
         "priority": 0, "created_at": "2026-01-02T00:00:00Z", "state_type": "unstarted",
         "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"], "id": "u2"},
    ]
    monkeypatch.setattr(conversation, "fetch_labelled_issues", lambda *a, **k: issues)
    return issues


def _model(monkeypatch, reply: str):
    monkeypatch.setattr(conversation, "chat", lambda *a, **k: reply)


def test_the_queue_is_given_to_the_model(cfg, queue, monkeypatch):
    """Without the queue in the prompt, 'the drizzle one' cannot resolve and
    the model is free to invent an id."""
    seen = {}

    def fake_chat(endpoint, model, prompt, **kwargs):
        seen["prompt"] = prompt
        return '{"action":"bump","ticket":"NIK-115","confidence":0.9}'
    monkeypatch.setattr(conversation, "chat", fake_chat)
    conversation.interpret(cfg, "do the drizzle one next")
    assert "NIK-115" in seen["prompt"] and "Migrate to Drizzle" in seen["prompt"]


def test_a_confident_intent_is_returned(cfg, queue, monkeypatch):
    _model(monkeypatch, '{"action":"bump","ticket":"NIK-115","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "do the drizzle one next")
    assert intent == Intent("bump", "NIK-115", 0.9) and question == ""


def test_low_confidence_asks_instead_of_acting(cfg, queue, monkeypatch):
    """Below local.min_confidence the bot must ask. Acting on a guess is how
    the wrong ticket gets parked overnight."""
    _model(monkeypatch, '{"action":"skip","ticket":"NIK-111","confidence":0.2}')
    intent, question = conversation.interpret(cfg, "maybe park something")
    assert intent is None and "not sure" in question.lower()


def test_an_unknown_action_asks(cfg, queue, monkeypatch):
    _model(monkeypatch, '{"action":"unknown","ticket":"","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "what is the airspeed of a swallow")
    assert intent is None and question


def test_a_ticket_outside_the_queue_is_refused(cfg, queue, monkeypatch):
    """The model can hallucinate an id that parses perfectly."""
    _model(monkeypatch, '{"action":"bump","ticket":"NIK-999","confidence":0.95}')
    intent, question = conversation.interpret(cfg, "bump 999")
    assert intent is None and "NIK-999" in question


def test_ollama_being_down_degrades_to_a_hint(cfg, queue, monkeypatch):
    """A local model outage must not look like the bot ignoring you."""
    def boom(*a, **k):
        raise OllamaError("connection refused")
    monkeypatch.setattr(conversation, "chat", boom)
    intent, question = conversation.interpret(cfg, "bump the drizzle one")
    assert intent is None and "/ralph" in question


def test_linear_being_down_degrades_to_a_hint(cfg, monkeypatch):
    from ralph.linear import LinearError

    def boom(*a, **k):
        raise LinearError("Linear request failed")
    monkeypatch.setattr(conversation, "fetch_labelled_issues", boom)
    intent, question = conversation.interpret(cfg, "bump the drizzle one")
    assert intent is None and "queue" in question.lower()


def test_read_only_actions_need_no_ticket(cfg, queue, monkeypatch):
    _model(monkeypatch, '{"action":"status","ticket":"","confidence":0.95}')
    intent, question = conversation.interpret(cfg, "what are you doing")
    assert intent is not None and intent.action == "status"


# --- Finding 1: unskip validates against the skipped set, not the queue ----

@pytest.fixture
def queue_with_backlog(monkeypatch):
    """One eligible (unstarted) ticket and one parked in Backlog, so unskip
    has something real to target and bump/skip still have a queue."""
    issues = [
        {"identifier": "NIK-111", "title": "Add haptics to breathing timer",
         "priority": 0, "created_at": "2026-01-01T00:00:00Z", "state_type": "unstarted",
         "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"], "id": "u1"},
        {"identifier": "NIK-222", "title": "Skipped ticket", "priority": 0,
         "created_at": "2026-01-03T00:00:00Z", "state_type": "backlog",
         "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"], "id": "u3"},
    ]
    monkeypatch.setattr(conversation, "fetch_labelled_issues", lambda *a, **k: issues)
    return issues


def test_unskip_of_a_backlog_ticket_is_accepted(cfg, queue_with_backlog, monkeypatch):
    """NIK-222 is not in the eligible queue (it is Backlog) -- that must not
    refuse an unskip the way it would refuse a bump."""
    _model(monkeypatch, '{"action":"unskip","ticket":"NIK-222","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "unskip NIK-222")
    assert intent == Intent("unskip", "NIK-222", 0.9) and question == ""


def test_unskip_of_an_unlisted_ticket_is_refused(cfg, queue_with_backlog, monkeypatch):
    """Neither queued (unstarted) nor skipped (backlog) -- unskip must still refuse."""
    _model(monkeypatch, '{"action":"unskip","ticket":"NIK-999","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "unskip NIK-999")
    assert intent is None and "NIK-999" in question and "not skipped" in question


def test_bump_still_validates_against_the_eligible_queue(cfg, queue_with_backlog, monkeypatch):
    """A Backlog ticket is not eligible for bump even though it exists."""
    _model(monkeypatch, '{"action":"bump","ticket":"NIK-222","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "bump NIK-222")
    assert intent is None and "not in the queue" in question


def test_skip_still_validates_against_the_eligible_queue(cfg, queue_with_backlog, monkeypatch):
    _model(monkeypatch, '{"action":"skip","ticket":"NIK-111","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "skip NIK-111")
    assert intent == Intent("skip", "NIK-111", 0.9) and question == ""


# --- Findings 2 & 3: which actions need a human confirmation click ---------

def test_needs_confirmation_matches_the_direction_of_safety_split():
    from ralph.ollama import ACTIONS

    expected = {
        "bump": True, "skip": True, "unskip": True, "go": True,
        "list": False, "status": False, "stop": False, "unknown": False,
    }
    assert set(expected) == set(ACTIONS)
    for action, expect in expected.items():
        assert conversation.needs_confirmation(action) is expect, action


def test_needs_confirmation_set_matches_the_predicate():
    from ralph.ollama import ACTIONS

    for action in ACTIONS:
        assert conversation.needs_confirmation(action) == (
            action in conversation.NEEDS_CONFIRMATION)


# --- Finding 4: a broken local.* config degrades to a message, not a crash -

def test_missing_local_config_key_degrades_to_a_hint(cfg, queue, monkeypatch):
    """A misspelled or missing local.* key must not raise KeyError up into
    the Slack listener, which would silently drop the human's message."""
    monkeypatch.delitem(cfg.local, "endpoint")
    intent, question = conversation.interpret(cfg, "bump the drizzle one")
    assert intent is None and "config is broken" in question


def test_non_numeric_num_ctx_degrades_to_a_hint(cfg, queue, monkeypatch):
    monkeypatch.setitem(cfg.local, "num_ctx", "not-a-number")
    intent, question = conversation.interpret(cfg, "bump the drizzle one")
    assert intent is None and "config is broken" in question


# --- saying why, when the queue is empty -------------------------------------

def test_an_unreadable_message_says_so_when_the_queue_has_work(cfg, queue, monkeypatch):
    """With work queued, 'I did not follow that' is the whole truth."""
    _model(monkeypatch, '{"action":"unknown","ticket":"","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "asdfgh")
    assert intent is None
    assert "queue" not in question.lower(), "no need to mention a queue that has work"


def test_an_unreadable_message_mentions_an_empty_queue(cfg, monkeypatch):
    """'start working on the next one' against an empty queue parses as unknown,
    because there is no next one. Replying only 'I did not follow that'
    misdiagnoses it: the sentence was fine, there is simply nothing to act on."""
    monkeypatch.setattr(conversation, "fetch_labelled_issues", lambda *a, **k: [])
    _model(monkeypatch, '{"action":"unknown","ticket":"","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "start working on the next one")
    assert intent is None
    assert "nothing" in question.lower() and "queue" in question.lower()


def test_a_low_confidence_reply_also_mentions_an_empty_queue(cfg, monkeypatch):
    monkeypatch.setattr(conversation, "fetch_labelled_issues", lambda *a, **k: [])
    _model(monkeypatch, '{"action":"bump","ticket":"NIK-1","confidence":0.1}')
    intent, question = conversation.interpret(cfg, "bump something")
    assert intent is None and "nothing" in question.lower()


def test_the_prompt_teaches_colloquial_phrasings():
    """'what's up' reached the model as unknown because nothing in the prompt
    connected everyday phrasing to an action."""
    from ralph.ollama import INTENT_PROMPT
    lowered = INTENT_PROMPT.lower()
    assert "what's up" in lowered or "whats up" in lowered
    assert "examples" in lowered
