"""Static checks on the shell entry points.

`bash -n` only validates syntax. Calling a helper that was never defined is a
*runtime* failure, so it survives syntax checks and only shows up mid-run --
which is exactly how `to_posix` reached a live run inside iterate.sh.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ["dispatch.sh", "iterate.sh"]

# Words that look like local helpers but are real programs or builtins.
EXTERNAL = {
    "cd", "echo", "exit", "set", "log", "mkdir", "rmdir", "trap", "printf",
    "source", "read", "cat", "date", "git", "claude", "timeout", "python",
    "tee", "grep", "sed", "awk", "mapfile", "command", "chmod", "rm", "if",
    "then", "else", "fi", "for", "while", "do", "done", "case", "esac", "local",
}


def script_text(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def defined_functions(text: str) -> set[str]:
    return set(re.findall(r"^([a-z_][a-z0-9_]*)\s*\(\)", text, re.MULTILINE))


def called_helpers(text: str) -> set[str]:
    """Helper-looking invocations: `$(name ...)` and line-leading `name ...`."""
    calls = set(re.findall(r"\$\(\s*([a-z_][a-z0-9_]*)\s+", text))
    calls |= set(re.findall(r"^\s*([a-z_][a-z0-9_]*)\s+[\"'$-]", text, re.MULTILINE))
    return calls


@pytest.mark.parametrize("name", SCRIPTS)
def test_syntax_is_valid(name):
    bash = shutil.which("bash")
    if not bash:
        pytest.skip("bash not available")
    proc = subprocess.run([bash, "-n", str(ROOT / name)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize("name", SCRIPTS)
def test_no_undefined_helpers(name):
    """Every helper-looking call is either defined here or a real program."""
    text = script_text(name)
    defined = defined_functions(text)
    unresolved = set()
    for call in called_helpers(text):
        if call in defined or call in EXTERNAL:
            continue
        if shutil.which(call):
            continue
        unresolved.add(call)
    assert not unresolved, f"{name} calls undefined helper(s): {sorted(unresolved)}"


@pytest.mark.parametrize("name", SCRIPTS)
def test_refuses_to_run_with_an_api_key(name):
    """Both entry points must independently enforce the billing guardrail:
    iterate.sh can be run by hand without dispatch.sh having checked."""
    assert "ANTHROPIC_API_KEY" in script_text(name)


def test_iterate_runs_claude_in_the_checkout():
    """Claude's sandbox follows its cwd. Invoked from the ralph project it cannot
    touch the repo it is meant to change, and reports success having done nothing."""
    text = script_text("iterate.sh")
    assert 'cd "$REPO_DIR_POSIX"' in text


def test_iterate_enforces_a_timeout():
    """The only enforced one-increment bound: there is no --max-turns flag."""
    assert "timeout --kill-after" in script_text("iterate.sh")


def test_iterate_does_not_grant_push_or_merge_tools():
    """The agent must not be able to push, PR, or merge -- the wrapper does that."""
    text = script_text("iterate.sh")
    for forbidden in ("Bash(git push", "Bash(gh ", "Bash(git merge"):
        assert forbidden not in text


# --- three-tier routing in dispatch.sh --------------------------------------

def test_dispatch_runs_triage_before_spending_a_claude_turn():
    """The whole point of tier 1: the counter must not be bumped before triage."""
    text = script_text("dispatch.sh")
    triage_at = text.index("local_triage.py")
    bump_at = text.index("runs.py bump")
    assert triage_at < bump_at, "triage must run before the run counter is bumped"


def test_dispatch_fails_open_when_triage_breaks():
    """Ollama being down must not silently drop a human-queued ticket."""
    text = script_text("dispatch.sh")
    assert '"run":true' in text.replace(" ", "")
    assert "routing to Claude" in text


def test_tier_zero_runs_before_tier_two():
    text = script_text("dispatch.sh")
    assert text.index("autofix.sh") < text.index("iterate.sh")


def test_tier_zero_escalates_rather_than_swallowing_a_ticket():
    """'nothing to fix' must fall through to Claude, not end the tick."""
    text = script_text("dispatch.sh")
    assert "escalating to Claude" in text


def test_autofix_verifies_before_committing():
    """A 'free' tier that lands a red branch is not free."""
    text = script_text("autofix.sh")
    assert "TEST_RC" in text and "reverting" in text


def test_autofix_distinguishes_broken_toolchain_from_clean():
    """Missing node_modules once made lint fail and look like 'nothing to fix'."""
    text = script_text("autofix.sh")
    assert "ERR_PNPM" in text and "not recognized" in text
    assert "INSTALL_CMD" in text


def test_dry_run_does_not_strand_the_ticket():
    """Dry run must not move a ticket to In Progress it will never move out of."""
    text = script_text("iterate.sh")
    assert "RALPH_DRY_RUN" in text
    in_progress = text.index("in_progress_state")
    guard = text.index('if [[ -z "${RALPH_DRY_RUN:-}" ]]')
    assert guard < in_progress, "the In Progress transition must sit inside the dry-run guard"
