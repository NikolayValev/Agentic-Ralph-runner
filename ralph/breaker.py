"""Phase 7: circuit breaker and log retention.

An unattended loop that fails the same way every tick is worse than one that
stops: it burns scarce subscription turns and fills Linear with noise. After N
consecutive unproductive runs the breaker trips the STOP file, and only a human
clears it.

"Unproductive" means error or no-commit -- a run that produced a reviewable PR
resets the count, whatever else happened during it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ralph.config import STATE_DIR, STOP_FILE

COUNTER = "consecutive-failures"
PRODUCTIVE = ("in_review",)


@dataclass(frozen=True)
class BreakerResult:
    consecutive: int
    tripped: bool
    reason: str


def _counter_path(state_dir: Path | None = None) -> Path:
    return (state_dir or STATE_DIR) / COUNTER


def read_count(state_dir: Path | None = None) -> int:
    path = _counter_path(state_dir)
    if not path.exists():
        return 0
    try:
        return int(path.read_text(encoding="utf-8").strip() or 0)
    except ValueError:
        return 0        # a corrupt counter must not wedge the loop; treat as fresh


def reset(state_dir: Path | None = None) -> None:
    path = _counter_path(state_dir)
    if path.exists():
        path.unlink()


def record(
    status: str, threshold: int, *, state_dir: Path | None = None,
    stop_file: Path | None = None,
) -> BreakerResult:
    """Record one run's outcome; trip the breaker if the streak hits threshold."""
    state_dir = state_dir or STATE_DIR
    stop_file = stop_file or STOP_FILE

    if status in PRODUCTIVE:
        reset(state_dir)
        return BreakerResult(0, False, "productive run; failure streak reset")

    count = read_count(state_dir) + 1
    state_dir.mkdir(parents=True, exist_ok=True)
    _counter_path(state_dir).write_text(str(count), encoding="utf-8")

    if count < threshold:
        return BreakerResult(count, False, f"{count}/{threshold} consecutive unproductive runs")

    reason = (
        f"circuit breaker tripped: {count} consecutive unproductive runs "
        f"(threshold {threshold}). Last status: {status}. "
        f"Investigate, then clear this file to resume."
    )
    stop_file.parent.mkdir(parents=True, exist_ok=True)
    stop_file.write_text(reason + "\n", encoding="utf-8")
    return BreakerResult(count, True, reason)


def prune_logs(log_dir: Path, keep_days: int) -> list[Path]:
    """Delete run artifacts older than keep_days. Returns what was removed.

    Only touches files this system writes; .gitkeep and anything unrecognised
    is left alone.
    """
    if keep_days <= 0 or not log_dir.exists():
        return []
    cutoff = time.time() - keep_days * 86400
    removed = []
    for path in log_dir.iterdir():
        if not path.is_file() or path.name == ".gitkeep":
            continue
        if not path.name.startswith(("dispatch-", "run-", "agent-", "prompt-")):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed
