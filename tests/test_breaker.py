"""Phase 7 AC: 3 simulated failures -> STOP. Plus log retention."""

from __future__ import annotations

import time

import pytest

from ralph.breaker import prune_logs, read_count, record, reset


@pytest.fixture
def state(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def stop_file(state):
    return state / "STOP"


def test_three_failures_trip_the_breaker(state):
    """The stated acceptance criterion."""
    results = [record("error", 3, state_dir=state, stop_file=stop_file(state))
               for _ in range(3)]
    assert [r.consecutive for r in results] == [1, 2, 3]
    assert [r.tripped for r in results] == [False, False, True]
    assert stop_file(state).exists()
    assert "circuit breaker tripped" in stop_file(state).read_text(encoding="utf-8")


def test_does_not_trip_early(state):
    for _ in range(2):
        record("error", 3, state_dir=state, stop_file=stop_file(state))
    assert not stop_file(state).exists()


def test_a_productive_run_resets_the_streak(state):
    """Two failures then a PR must not leave the loop one failure from stopping."""
    record("error", 3, state_dir=state, stop_file=stop_file(state))
    record("blocked", 3, state_dir=state, stop_file=stop_file(state))
    assert read_count(state) == 2

    result = record("in_review", 3, state_dir=state, stop_file=stop_file(state))
    assert result.consecutive == 0 and not result.tripped
    assert read_count(state) == 0

    for _ in range(2):
        record("error", 3, state_dir=state, stop_file=stop_file(state))
    assert not stop_file(state).exists()


def test_blocked_counts_as_unproductive(state):
    """No commit is no progress, whatever the agent called it."""
    for _ in range(3):
        record("blocked", 3, state_dir=state, stop_file=stop_file(state))
    assert stop_file(state).exists()


def test_mixed_failure_kinds_still_trip(state):
    for status in ("error", "blocked", "error"):
        result = record(status, 3, state_dir=state, stop_file=stop_file(state))
    assert result.tripped


def test_corrupt_counter_does_not_wedge_the_loop(state):
    (state / "consecutive-failures").write_text("garbage", encoding="utf-8")
    assert read_count(state) == 0
    assert record("error", 3, state_dir=state, stop_file=stop_file(state)).consecutive == 1


def test_reset_is_safe_when_absent(state):
    reset(state)  # must not raise


def test_threshold_of_one_trips_immediately(state):
    assert record("error", 1, state_dir=state, stop_file=stop_file(state)).tripped


# --- log retention ----------------------------------------------------------

def test_prunes_only_old_run_artifacts(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    old, new = time.time() - 30 * 86400, time.time()

    for name, mtime in [("run-old.log", old), ("agent-old.txt", old),
                        ("prompt-old.md", old), ("dispatch-old.log", old),
                        ("run-new.log", new)]:
        p = logs / name
        p.write_text("x", encoding="utf-8")
        import os
        os.utime(p, (mtime, mtime))

    keep = logs / ".gitkeep"
    keep.write_text("", encoding="utf-8")
    import os
    os.utime(keep, (old, old))

    unrelated = logs / "notes.txt"
    unrelated.write_text("x", encoding="utf-8")
    os.utime(unrelated, (old, old))

    removed = {p.name for p in prune_logs(logs, keep_days=14)}
    assert removed == {"run-old.log", "agent-old.txt", "prompt-old.md", "dispatch-old.log"}
    assert keep.exists(), ".gitkeep must survive"
    assert unrelated.exists(), "unrecognised files must not be deleted"
    assert (logs / "run-new.log").exists()


def test_retention_disabled_removes_nothing(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "run-old.log").write_text("x", encoding="utf-8")
    assert prune_logs(logs, keep_days=0) == []
