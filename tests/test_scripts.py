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


# --- the human must hear about the runs that failed -------------------------

def test_finalize_failure_still_reaches_notify():
    """finalize exits 1 on outcomes that still print a report and still matter.

    "modified a forbidden path" and "branch pushed but the PR failed" both emit
    a report and return 1. iterate.sh runs under `set -euo pipefail`, so without
    errexit disabled across the finalize pipeline the script dies on that line:
    FINAL_RC is never read and notify never runs, making the failures worth
    hearing about the only silent ones.
    """
    text = script_text("iterate.sh")
    finalize_at = text.index("finalize.py --ticket")
    notify_at = text.index("notify.py --report-file")

    guard_off = text.rindex("set +e", 0, finalize_at)
    guard_on = text.index("set -e", finalize_at)
    assert guard_off < finalize_at < guard_on < notify_at, (
        "the finalize pipeline must run with errexit off, restored before notify"
    )
    assert "PIPESTATUS[0]" in text, (
        "through a pipe $? is tee's status; a failed finalize would read as success"
    )


def test_notify_cannot_fail_the_tick():
    """A Slack outage must not turn a completed run into a failed one."""
    text = script_text("iterate.sh")
    assert "if ! " in text[text.index("notify.py --report-file") - 120:], (
        "notify must be invoked in a condition, not as a bare fatal command"
    )


def test_iterate_notifies_after_finalize():
    """Phase 4 shipped unwired: nothing called notify.py for a full day."""
    text = script_text("iterate.sh")
    assert "notify.py" in text, "the tick must tell the human what it did"
    # Compare the invocations, not the first mention: the comment above the
    # finalize call names notify.py, so a naive index() search reads backwards.
    assert text.index("finalize.py --ticket") < text.index("notify.py --report-file"), (
        "notify consumes finalize's report, so it must run after it"
    )


def test_shell_scripts_are_lf_only():
    """A CRLF .sh breaks under any bash that is not msys2's.

    Git Bash tolerates a CRLF shebang; WSL's bash does not, and no bash
    tolerates `set -euo pipefail\r` -- it reports "invalid option name".
    Editing these files with a tool that translates newlines on Windows
    (Python's text-mode write does) silently reintroduces this.
    """
    for name in ROOT.glob("*.sh"):
        raw = name.read_bytes()
        assert b"\x0d\x0a" not in raw, (
            f"{name.name} has CRLF line endings; rewrite it in binary mode"
        )
