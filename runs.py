#!/usr/bin/env python
"""Per-day Tier-2 run counter (sec.7 state files).

The counter tracks *Claude* runs, not ticks: a tick resolved locally at Tier 0/1
costs no subscription usage and must not consume the day's budget.
"""

from __future__ import annotations

import argparse
import os
import sys

from ralph.config import configure_stdio, ConfigError, STOP_FILE, load


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ralph run counter / kill switch")
    parser.add_argument("action", choices=["show", "bump", "stop", "go", "prune", "streak"])
    parser.add_argument("--config")
    args = parser.parse_args(argv)
    configure_stdio()

    try:
        cfg = load(args.config) if args.config else load(check_env=False)
    except ConfigError as exc:
        print(f"runs: config error: {exc}", file=sys.stderr)
        return 1

    if args.action == "show":
        print(f"{cfg.runs_today()}/{cfg.max_runs_per_day}")
    elif args.action == "bump":
        if cfg.cap_reached():
            print(
                f"runs: refusing to bump past the cap "
                f"({cfg.runs_today()}/{cfg.max_runs_per_day})",
                file=sys.stderr,
            )
            return 1
        print(f"{cfg.increment_runs_today()}/{cfg.max_runs_per_day}")
    elif args.action == "stop":
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text("stopped via `runs.py stop`\n", encoding="utf-8")
        print(f"STOP created at {STOP_FILE}")
    elif args.action == "prune":
        from pathlib import Path

        from ralph.breaker import prune_logs
        days = int(cfg.raw["safety"].get("log_retention_days", 14))
        log_dir = Path(os.environ.get("RALPH_LOG_DIR") or (Path(__file__).parent / "logs"))
        removed = prune_logs(log_dir, days)
        print(f"pruned {len(removed)} artifact(s) older than {days}d from {log_dir}")
    elif args.action == "streak":
        from ralph.breaker import read_count
        threshold = int(cfg.raw["safety"].get("circuit_breaker_threshold", 3))
        print(f"{read_count()}/{threshold} consecutive unproductive runs")
    elif args.action == "go":
        if STOP_FILE.exists():
            STOP_FILE.unlink()
            print(f"STOP cleared at {STOP_FILE}")
        else:
            print("no STOP file present")
    return 0


if __name__ == "__main__":
    sys.exit(main())
