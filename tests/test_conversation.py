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
