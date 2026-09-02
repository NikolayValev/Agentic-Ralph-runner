#!/usr/bin/env python
"""Phase 4: post a run report to Slack.

Reads an Agent Report JSON on stdin (or --report-file) and posts it.
A `nothing_eligible` report posts nothing and exits 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ralph.config import ConfigError, configure_stdio, load
from ralph.contracts import AgentReport, ContractError
from ralph.slack import build_message, should_notify


class NotifyError(RuntimeError):
    pass


def post(payload: dict, token: str) -> str:
    """Send to Slack; returns the message ts. Import is local so the module
    stays importable (and testable) on a machine without slack_sdk."""
    if not token:
        raise NotifyError("SLACK_BOT_TOKEN is not set")
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        response = WebClient(token=token).chat_postMessage(**payload)
    except SlackApiError as exc:
        raise NotifyError(f"Slack rejected the message: {exc.response['error']}") from exc
    return response["ts"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post a run report to Slack")
    parser.add_argument("--report-file", help="read the report from a file instead of stdin")
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the payload instead of sending it")
    args = parser.parse_args(argv)
    configure_stdio()

    try:
        cfg = load(args.config) if args.config else load()
    except ConfigError as exc:
        print(f"notify: config error: {exc}", file=sys.stderr)
        return 1

    raw = (open(args.report_file, encoding="utf-8").read() if args.report_file
           else sys.stdin.read())
    try:
        payload = json.loads(raw.strip().splitlines()[-1] if raw.strip() else "{}")
        report = AgentReport.parse(payload)
    except (ValueError, IndexError, ContractError) as exc:
        print(f"notify: not a valid report: {exc}", file=sys.stderr)
        return 1

    if not should_notify(report):
        print(f"notify: status {report.status!r} is silent; nothing posted", file=sys.stderr)
        return 0

    channel = os.environ.get("SLACK_CHANNEL", "")
    if not channel:
        print("notify: SLACK_CHANNEL is not set", file=sys.stderr)
        return 1

    message = build_message(report, channel=channel)
    if args.dry_run:
        print(json.dumps(message, indent=2))
        return 0

    try:
        ts = post(message, os.environ.get("SLACK_BOT_TOKEN", ""))
    except NotifyError as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 1
    print(f"notify: posted to {channel} (ts={ts})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
