"""Strict parsing of the local model's intent JSON.

Mirrors parse_verdict: a vague answer is not a decision. Everything here is
pure string -> Intent; no network, no Slack.
"""

from __future__ import annotations

import pytest

from ralph.ollama import Intent, OllamaError, parse_intent


def test_parses_a_clean_intent():
    got = parse_intent('{"action":"bump","ticket":"NIK-105","confidence":0.9}')
    assert got == Intent(action="bump", ticket="NIK-105", confidence=0.9)


def test_extracts_json_from_surrounding_prose():
    """gpt-oss is a reasoning model and sometimes narrates around its answer."""
    raw = 'Sure! Here is the result:\n{"action":"skip","ticket":"NIK-1","confidence":0.7}\nHope that helps.'
    assert parse_intent(raw).action == "skip"


def test_an_unknown_action_becomes_unknown_not_an_error():
    """A model inventing an action must degrade to a clarifying question, not
    a crash that takes the listener down."""
    got = parse_intent('{"action":"deploy","ticket":"NIK-1","confidence":0.9}')
    assert got.action == "unknown"


def test_a_missing_confidence_is_zero_not_one():
    """Defaulting high would let a silent model authorise a write."""
    assert parse_intent('{"action":"bump","ticket":"NIK-1"}').confidence == 0.0


def test_confidence_is_clamped():
    assert parse_intent('{"action":"bump","ticket":"NIK-1","confidence":7}').confidence == 1.0
    assert parse_intent('{"action":"bump","ticket":"NIK-1","confidence":-3}').confidence == 0.0


def test_the_ticket_is_uppercased_and_trimmed():
    assert parse_intent('{"action":"bump","ticket":" nik-105 ","confidence":0.9}').ticket == "NIK-105"


def test_no_json_at_all_raises():
    with pytest.raises(OllamaError, match="no JSON"):
        parse_intent("I'm not sure what you mean")


def test_malformed_json_raises():
    with pytest.raises(OllamaError, match="invalid JSON"):
        parse_intent('{"action":"bump", ticket:}')
