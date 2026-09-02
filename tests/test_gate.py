"""Phase 1 AC: correct task JSON for a tagged ticket; exits when none;
the run-count cap and STOP both halt the tick."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import yaml

from ralph.linear import is_eligible, select_ticket

ROOT = Path(__file__).resolve().parent.parent
ELIGIBLE = "autonomous-eligible"
REPO = "repo:quitting-smoking-tracker"

EXIT_OK, EXIT_ERROR, EXIT_NOTHING, EXIT_WINDOW, EXIT_CAP, EXIT_STOP = 0, 1, 10, 11, 12, 13


def issue(identifier, *, labels=(ELIGIBLE, REPO), state=("Todo", "unstarted"), created="2026-08-01T00:00:00Z"):
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"title for {identifier}",
        "created_at": created,
        "state_name": state[0],
        "state_type": state[1],
        "labels": list(labels),
    }


# --- pure selection rules ---------------------------------------------------

def test_picks_oldest_eligible():
    issues = [
        issue("NIK-3", created="2026-08-03T00:00:00Z"),
        issue("NIK-1", created="2026-08-01T00:00:00Z"),
        issue("NIK-2", created="2026-08-02T00:00:00Z"),
    ]
    ticket, _ = select_ticket(issues, eligible_label=ELIGIBLE, repo_label=REPO)
    assert ticket["identifier"] == "NIK-1"


def test_ties_broken_deterministically():
    same = "2026-08-01T00:00:00Z"
    a = select_ticket([issue("NIK-9", created=same), issue("NIK-4", created=same)],
                      eligible_label=ELIGIBLE, repo_label=REPO)[0]
    b = select_ticket([issue("NIK-4", created=same), issue("NIK-9", created=same)],
                      eligible_label=ELIGIBLE, repo_label=REPO)[0]
    assert a["identifier"] == b["identifier"] == "NIK-4"


def test_backlog_ticket_is_parked_not_queued():
    """A labelled ticket left in Backlog must NOT be picked up.

    Opting in is deliberately two gestures: label it AND move it to Todo.
    """
    issues = [issue("NIK-1", state=("Backlog", "backlog"))]
    ticket, skipped = select_ticket(issues, eligible_label=ELIGIBLE, repo_label=REPO)
    assert ticket is None
    assert "not queued" in skipped[0]


@pytest.mark.parametrize("state", [
    ("In Progress", "started"), ("In Review", "started"),
    ("Done", "completed"), ("Canceled", "canceled"), ("Backlog", "backlog"),
])
def test_only_unstarted_is_eligible(state):
    ok, _ = is_eligible(issue("NIK-1", state=state), eligible_label=ELIGIBLE, repo_label=REPO)
    assert ok is False


def test_missing_opt_in_label_is_skipped():
    ok, reason = is_eligible(issue("NIK-1", labels=(REPO,)), eligible_label=ELIGIBLE, repo_label=REPO)
    assert ok is False and ELIGIBLE in reason


def test_wrong_repo_label_is_skipped():
    """A ticket for another repo must not be picked up by this instance."""
    other = issue("NIK-1", labels=(ELIGIBLE, "repo:mandate-zero"))
    ok, reason = is_eligible(other, eligible_label=ELIGIBLE, repo_label=REPO)
    assert ok is False and REPO in reason


def test_no_issues_at_all():
    ticket, skipped = select_ticket([], eligible_label=ELIGIBLE, repo_label=REPO)
    assert ticket is None and skipped == []


# --- gate.py end-to-end -----------------------------------------------------

@pytest.fixture
def sandbox(tmp_path):
    """An isolated state dir + config so gate runs never touch real state."""
    state = tmp_path / "state"
    state.mkdir()
    raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8"))

    def write_config(**schedule):
        raw["schedule"].update(schedule)
        path = tmp_path / "config.yaml"
        path.write_text(yaml.safe_dump(raw), encoding="utf-8")
        return path

    def run(args, config=None, drop_env=()):
        env = dict(os.environ, RALPH_STATE_DIR=str(state), PYTHONPATH=str(ROOT))
        env.pop("ANTHROPIC_API_KEY", None)
        for name in drop_env:
            env.pop(name, None)
        return subprocess.run(
            [sys.executable, str(ROOT / "gate.py"), "--config", str(config or write_config())] + args,
            capture_output=True, text=True, cwd=ROOT, env=env, timeout=120,
        )

    def fixture_file(issues):
        path = tmp_path / "issues.json"
        path.write_text(json.dumps(issues), encoding="utf-8")
        return str(path)

    ns = type("Sandbox", (), {})()
    ns.state, ns.run, ns.write_config, ns.fixture_file = state, run, write_config, fixture_file
    return ns


def _always_open_window():
    """A window guaranteed to contain 'now', whenever the suite runs."""
    now = datetime.now()
    start = (now - timedelta(hours=1)).strftime("%H:00")
    end = (now + timedelta(hours=2)).strftime("%H:00")
    return start + "-" + end


def test_emits_task_json_for_tagged_ticket(sandbox):
    """AC: a tagged test ticket yields correct Task JSON on stdout."""
    issues = sandbox.fixture_file([issue("NIK-104"), issue("NIK-108", created="2026-08-09T00:00:00Z")])
    result = sandbox.run(["--ignore-schedule", "--issues-file", issues])
    assert result.returncode == EXIT_OK, result.stderr
    assert json.loads(result.stdout.strip()) == {"mode": "tagged", "ref": "NIK-104"}


def test_exits_when_nothing_eligible(sandbox):
    """AC: exits non-zero and prints nothing on stdout when there is no work."""
    issues = sandbox.fixture_file([issue("NIK-104", state=("Backlog", "backlog"))])
    result = sandbox.run(["--ignore-schedule", "--issues-file", issues])
    assert result.returncode == EXIT_NOTHING
    assert result.stdout.strip() == ""


def test_stop_file_halts(sandbox):
    """AC: STOP halts the tick, ahead of any other consideration."""
    (sandbox.state / "STOP").write_text("paused by test", encoding="utf-8")
    issues = sandbox.fixture_file([issue("NIK-104")])
    result = sandbox.run(["--ignore-schedule", "--issues-file", issues])
    assert result.returncode == EXIT_STOP
    assert result.stdout.strip() == ""
    assert "STOP" in result.stderr


def test_run_cap_halts(sandbox):
    """AC: the per-day run cap is a hard stop even with work waiting."""
    config = sandbox.write_config(windows=[_always_open_window()], max_runs_per_day=3)
    (sandbox.state / f"runs-{datetime.now().date().isoformat()}").write_text("3", encoding="utf-8")
    issues = sandbox.fixture_file([issue("NIK-104")])
    result = sandbox.run(["--issues-file", issues], config=config)
    assert result.returncode == EXIT_CAP
    assert result.stdout.strip() == ""
    assert "cap reached" in result.stderr


def test_under_cap_proceeds(sandbox):
    config = sandbox.write_config(windows=[_always_open_window()], max_runs_per_day=3)
    (sandbox.state / f"runs-{datetime.now().date().isoformat()}").write_text("2", encoding="utf-8")
    issues = sandbox.fixture_file([issue("NIK-104")])
    result = sandbox.run(["--issues-file", issues], config=config)
    assert result.returncode == EXIT_OK, result.stderr


def test_outside_window_halts(sandbox):
    """AC: the schedule window is a hard stop."""
    now = datetime.now()
    closed = (now + timedelta(hours=3)).strftime("%H:00") + "-" + (now + timedelta(hours=4)).strftime("%H:00")
    config = sandbox.write_config(windows=[closed])
    issues = sandbox.fixture_file([issue("NIK-104")])
    result = sandbox.run(["--issues-file", issues], config=config)
    assert result.returncode == EXIT_WINDOW
    assert result.stdout.strip() == ""


def test_missing_api_key_is_an_error_not_a_silent_noop(sandbox):
    """Without a key the gate must fail loudly, not look like 'no work'."""
    env_result = sandbox.run(["--ignore-schedule"], drop_env=["LINEAR_API_KEY"])
    assert env_result.returncode == EXIT_ERROR
    assert "LINEAR_API_KEY" in env_result.stderr


def test_non_ascii_ticket_title_does_not_crash(sandbox):
    """Linear titles contain em dashes (NIK-106 does). Under a cp1252 console
    with output redirected to a log, an unconfigured stream raises
    UnicodeEncodeError and kills the tick over a dash in a title."""
    titled = issue("NIK-106")
    titled["title"] = "pnpm lint is broken — FlatCompat fails on eslint-config-next 16"
    result = sandbox.run(["--ignore-schedule", "--issues-file", sandbox.fixture_file([titled])])
    assert result.returncode == EXIT_OK, result.stderr
    assert json.loads(result.stdout.strip()) == {"mode": "tagged", "ref": "NIK-106"}
