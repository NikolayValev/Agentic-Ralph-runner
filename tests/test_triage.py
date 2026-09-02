"""Phase 6: the pre-filter saves turns without ever costing a ticket."""

from __future__ import annotations

import pytest

import local_triage
from ralph.config import load
from ralph.ollama import OllamaError, parse_verdict


def cfg(enabled=True, min_confidence=0.6):
    c = load(check_env=False)
    c.raw["local"]["enabled"] = enabled
    c.raw["local"]["min_confidence"] = min_confidence
    return c


# --- parsing the local model's answer ---------------------------------------

def test_parses_a_good_verdict():
    v = parse_verdict('{"run": true, "tier": "claude", "reason": "r", '
                      '"commit_hint": "c", "confidence": 0.9}')
    assert v.run is True and v.tier == "claude" and v.confidence == 0.9


def test_tolerates_surrounding_text():
    v = parse_verdict('Sure!\n{"run": false, "tier": "tooling", "reason": "r", '
                      '"commit_hint": "", "confidence": 0.8}\nHope that helps.')
    assert v.run is False


@pytest.mark.parametrize("raw", [
    "I think we should run it.",                       # prose, no JSON
    '{"run": "yes", "tier": "claude"}',                # run not a bool
    '{"run": true, "tier": "gpt4"}',                   # unknown tier
    '{"run": true, "tier": "claude", ',                # truncated
])
def test_rejects_a_non_answer(raw):
    """A vague answer is not a decision; it must raise so we can fail open."""
    with pytest.raises(OllamaError):
        parse_verdict(raw)


def test_confidence_is_clamped():
    v = parse_verdict('{"run": true, "tier": "claude", "reason": "", '
                      '"commit_hint": "", "confidence": 5}')
    assert v.confidence == 1.0


def test_non_numeric_confidence_becomes_zero():
    v = parse_verdict('{"run": true, "tier": "claude", "reason": "", '
                      '"commit_hint": "", "confidence": "high"}')
    assert v.confidence == 0.0


# --- the fail-open contract -------------------------------------------------

def test_disabled_tier_routes_to_claude():
    result = local_triage.triage(cfg(enabled=False), "NIK-1", "t", "d")
    assert result.run is True and result.tier == "claude"


def test_ollama_down_routes_to_claude(monkeypatch):
    """Ollama being unreachable must not silently drop a queued ticket."""
    def boom(*a, **k):
        raise OllamaError("connection refused")
    monkeypatch.setattr(local_triage, "chat", boom)
    result = local_triage.triage(cfg(), "NIK-1", "t", "d")
    assert result.run is True and result.tier == "claude"
    assert "unavailable" in result.reason


def test_garbage_answer_routes_to_claude(monkeypatch):
    monkeypatch.setattr(local_triage, "chat", lambda *a, **k: "I'm not sure really")
    assert local_triage.triage(cfg(), "NIK-1", "t", "d").run is True


def test_low_confidence_routes_to_claude(monkeypatch):
    """A hesitant skip is the dangerous one -- it silently loses work."""
    monkeypatch.setattr(local_triage, "chat", lambda *a, **k:
        '{"run": false, "tier": "claude", "reason": "meh", '
        '"commit_hint": "", "confidence": 0.2}')
    result = local_triage.triage(cfg(min_confidence=0.6), "NIK-1", "t", "d")
    assert result.run is True
    assert "confidence" in result.reason


def test_confident_skip_is_honoured(monkeypatch):
    monkeypatch.setattr(local_triage, "chat", lambda *a, **k:
        '{"run": false, "tier": "tooling", "reason": "just a question", '
        '"commit_hint": "", "confidence": 0.95}')
    assert local_triage.triage(cfg(), "NIK-1", "t", "d").run is False


def test_confident_tooling_route_is_honoured(monkeypatch):
    monkeypatch.setattr(local_triage, "chat", lambda *a, **k:
        '{"run": true, "tier": "tooling", "reason": "formatting only", '
        '"commit_hint": "style: format", "confidence": 0.9}')
    result = local_triage.triage(cfg(), "NIK-1", "t", "d")
    assert result.tier == "tooling" and result.commit_hint


def test_output_satisfies_the_prefilter_contract(monkeypatch):
    from ralph.contracts import PreFilter
    monkeypatch.setattr(local_triage, "chat", lambda *a, **k:
        '{"run": true, "tier": "claude", "reason": "r", '
        '"commit_hint": "c", "confidence": 0.9}')
    result = local_triage.triage(cfg(), "NIK-1", "t", "d")
    import json
    PreFilter.parse(json.loads(result.to_json()))   # must round-trip
