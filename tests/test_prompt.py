"""The prompt is the agent's whole program; a typo'd placeholder ships silently."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = (ROOT / "prompts" / "agent.md").read_text(encoding="utf-8")

# Exactly the substitutions iterate.sh performs.
SUBSTITUTED = {
    "{TICKET}", "{TITLE}", "{DESCRIPTION}", "{REPO_DIR}",
    "{BRANCH}", "{BASE}", "{TEST_CMD}", "{LINT_CMD}",
}


def test_every_placeholder_is_substituted():
    """A placeholder the wrapper does not fill reaches the model as literal text."""
    present = set(re.findall(r"\{[A-Z_]+\}", TEMPLATE))
    assert present <= SUBSTITUTED, f"unsubstituted placeholders: {present - SUBSTITUTED}"


def test_every_substitution_is_used():
    """A substitution with no placeholder means the agent never learns that fact."""
    present = set(re.findall(r"\{[A-Z_]+\}", TEMPLATE))
    assert SUBSTITUTED <= present, f"unused substitutions: {SUBSTITUTED - present}"


def test_filled_prompt_has_no_leftovers():
    filled = TEMPLATE
    for key in SUBSTITUTED:
        filled = filled.replace(key, "X")
    assert not re.search(r"\{[A-Z_]+\}", filled)


# --- the prohibitions must actually be stated -------------------------------

def test_prompt_forbids_the_dangerous_actions():
    """These are hard constraints (sec.4). The wrapper enforces them, but the
    prompt must not invite the agent to try."""
    lowered = TEMPLATE.lower()
    for forbidden in ("do not push", "do not merge", "do not deploy"):
        assert forbidden in lowered, f"prompt does not say {forbidden!r}"
    assert "do not open a pull request" in lowered


def test_prompt_protects_its_own_permissions():
    assert ".claude/" in TEMPLATE


def test_prompt_states_the_report_contract():
    assert '"status":"in_review"' in TEMPLATE
    assert "240" in TEMPLATE


def test_prompt_demands_verification():
    """'Verify before you claim' is the guard against a green report on a red suite."""
    assert "{TEST_CMD}" in TEMPLATE and "{LINT_CMD}" in TEMPLATE
    assert "never report success" in TEMPLATE.lower()
