#!/usr/bin/env python
"""Phase 1 - the gate. Read-only: decides whether this tick does anything.

Emits a Task JSON object (sec.7) on stdout and exits 0 when there is work.
Any other exit code means "do not run"; the reason goes to stderr.

Exit codes are distinct so dispatch.sh can log *why* a tick was a no-op:
  0  work found      -> Task JSON on stdout
  10 nothing eligible
  11 outside the schedule window
  12 per-day run cap reached
  13 STOP file present
  1  error (config, network, bad key)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime

from ralph.config import configure_stdio, STOP_FILE, ConfigError, load
from ralph.contracts import Task
from ralph.linear import LinearError, fetch_labelled_issues, select_ticket

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_NOTHING = 10
EXIT_WINDOW = 11
EXIT_CAP = 12
EXIT_STOP = 13


def log(message: str) -> None:
    print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ralph gate (read-only)")
    parser.add_argument(
        "--ignore-schedule",
        action="store_true",
        help="skip the window and per-day cap checks (manual testing only)",
    )
    parser.add_argument(
        "--issues-file",
        help="read issues from a JSON file instead of Linear (offline testing)",
    )
    parser.add_argument(
        "--explain", action="store_true", help="log why each ticket was skipped"
    )
    parser.add_argument("--config", help="alternate config.yaml (testing)")
    args = parser.parse_args(argv)
    configure_stdio()

    try:
        cfg = load(args.config) if args.config else load()
    except ConfigError as exc:
        log(f"gate: config error: {exc}")
        return EXIT_ERROR

    # --- guards, cheapest and most absolute first -------------------------
    if cfg.stopped():
        log(f"gate: STOP file present at {STOP_FILE}; not running")
        return EXIT_STOP

    if not args.ignore_schedule:
        now = datetime.now()
        if not cfg.in_window(now):
            windows = ", ".join(str(w) for w in cfg.windows)
            log(f"gate: {now:%H:%M} is outside the schedule windows ({windows})")
            return EXIT_WINDOW
        if cfg.cap_reached():
            log(
                f"gate: per-day run cap reached "
                f"({cfg.runs_today()}/{cfg.max_runs_per_day})"
            )
            return EXIT_CAP

    # --- find work --------------------------------------------------------
    eligible_label = cfg.linear["eligible_label"]
    repo_label = cfg.linear.get("repo_label", "")

    try:
        if args.issues_file:
            with open(args.issues_file, encoding="utf-8") as handle:
                issues = json.load(handle)
        else:
            issues = fetch_labelled_issues(
                os.environ.get("LINEAR_API_KEY", ""),
                cfg.linear["team_key"],
                eligible_label,
            )
    except LinearError as exc:
        log(f"gate: {exc}")
        return EXIT_ERROR
    except (OSError, ValueError) as exc:
        log(f"gate: could not read issues: {exc}")
        return EXIT_ERROR

    ticket, skipped = select_ticket(
        issues, eligible_label=eligible_label, repo_label=repo_label
    )

    if args.explain:
        for reason in skipped:
            log(f"gate: skipped {reason}")

    if ticket is None:
        log(
            f"gate: nothing eligible "
            f"({len(issues)} labelled {eligible_label!r}, {len(skipped)} skipped)"
        )
        return EXIT_NOTHING

    log(f"gate: selected {ticket['identifier']} - {ticket['title'][:80]}")
    print(Task(mode="tagged", ref=ticket["identifier"]).to_json())
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
