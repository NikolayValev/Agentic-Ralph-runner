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


def post(payload: dict, token: str) -> tuple[str, str]:
    """Send to Slack; returns (channel_id, ts).

    The channel ID matters as much as the ts: chat.update will not accept the
    "#ralph" name that chat.postMessage does, so the resolved id from the
    response is the only usable handle. Import is local so the module stays
    importable (and testable) on a machine without slack_sdk.
    """
    if not token:
        raise NotifyError("SLACK_BOT_TOKEN is not set")
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    try:
        response = WebClient(token=token).chat_postMessage(**payload)
    except SlackApiError as exc:
        raise NotifyError(f"Slack rejected the message: {exc.response['error']}") from exc
    return response["channel"], response["ts"]


def update(payload: dict, token: str, channel: str, ts: str) -> str:
    """Edit an existing message in place. Returns the ts it edited."""
    if not token:
        raise NotifyError("SLACK_BOT_TOKEN is not set")
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    edited = dict(payload, channel=channel, ts=ts)
    edited.pop("unfurl_links", None)      # chat.update rejects these
    edited.pop("unfurl_media", None)
    try:
        WebClient(token=token).chat_update(**edited)
    except SlackApiError as exc:
        raise NotifyError(f"Slack rejected the edit: {exc.response['error']}") from exc
    return ts


def write_handle(path: str, channel: str, ts: str) -> None:
    """Record where a message lives so a later run stage can edit it."""
    with open(path, "w", encoding="utf-8") as handle:
        json.dump({"channel": channel, "ts": ts}, handle)


def read_handle(path: str) -> tuple[str, str] | None:
    """Return (channel, ts), or None if there is no usable handle.

    A missing or corrupt handle is not an error: it means we post afresh.
    """
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data["channel"], data["ts"]
    except (OSError, ValueError, KeyError, TypeError):
        return None


def deliver(payload: dict, token: str, *, handle_path: str = "",
            emit_path: str = "") -> tuple[str, str]:
    """Edit the run's existing message if we have one, otherwise post.

    Falling back to a fresh post when the edit fails is deliberate: a duplicate
    message is a far better failure than a run that finished and told nobody.
    """
    handle = read_handle(handle_path) if handle_path else None
    if handle:
        channel, ts = handle
        try:
            update(payload, token, channel, ts)
            return channel, ts
        except NotifyError as exc:
            print(f"notify: edit failed ({exc}); posting a new message",
                  file=sys.stderr)

    channel, ts = post(payload, token)
    if emit_path:
        write_handle(emit_path, channel, ts)
    return channel, ts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post a run report to Slack")
    parser.add_argument("--report-file", help="read the report from a file instead of stdin")
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the payload instead of sending it")
    parser.add_argument("--emit-handle", metavar="FILE",
                        help="record where the posted message lives, so a later "
                             "stage can edit it instead of posting again")
    parser.add_argument("--update-handle", metavar="FILE",
                        help="edit the message recorded in FILE; posts a new one "
                             "if the handle is missing or the edit is refused")
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
        _, ts = deliver(
            message, os.environ.get("SLACK_BOT_TOKEN", ""),
            handle_path=args.update_handle or "",
            emit_path=args.emit_handle or "",
        )
    except NotifyError as exc:
        print(f"notify: {exc}", file=sys.stderr)
        return 1
    verb = "edited" if args.update_handle else "posted"
    print(f"notify: {verb} {channel} (ts={ts})", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
