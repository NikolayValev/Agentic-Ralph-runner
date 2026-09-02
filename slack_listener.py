#!/usr/bin/env python
"""Phase 5: long-running Slack listener (Socket Mode).

Handles `/ralph ...` and the Approve/Discard buttons out of band from the tick.
Runs continuously; dispatch.sh does not depend on it.
"""

from __future__ import annotations

import argparse
import os
import sys

from ralph.commands import (HELP, Reply, handle_approve, handle_bump,
                            handle_discard, handle_skip, handle_slash,
                            status_text)
from ralph.config import ConfigError, configure_stdio, load
from ralph.slack import ACTION_APPROVE, ACTION_BUMP, ACTION_DISCARD, ACTION_SKIP


def dispatch_action(cfg, action_id: str, ticket: str) -> Reply:
    if action_id == ACTION_APPROVE:
        return handle_approve(cfg, ticket)
    if action_id == ACTION_DISCARD:
        return handle_discard(cfg, ticket)
    if action_id == ACTION_BUMP:
        return handle_bump(cfg, ticket)
    if action_id == ACTION_SKIP:
        return handle_skip(cfg, ticket)
    return Reply(f"Unknown action `{action_id}`.")


def ticket_from_payload(payload: dict) -> str:
    """Prefer the button's own value; fall back to the block_id suffix."""
    actions = payload.get("actions") or []
    if actions and actions[0].get("value"):
        return actions[0]["value"]
    block_id = (actions[0].get("block_id") if actions else "") or ""
    return block_id.split("::")[-1] if "::" in block_id else ""


def _post_reply(web, channel: str, reply, user: str = "") -> None:
    """One place that knows how to turn a Reply into a Slack call.

    The previous inline ternary could not carry blocks, and an ephemeral reply
    needs a different method entirely.
    """
    payload = {"channel": channel, "text": reply.text}
    if reply.blocks:
        payload["blocks"] = reply.blocks
    if reply.ephemeral and user:
        web.chat_postEphemeral(user=user, **payload)
    else:
        web.chat_postMessage(**payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ralph Slack listener (Socket Mode)")
    parser.add_argument("--config")
    parser.add_argument("--once", metavar="COMMAND",
                        help="run one /ralph command locally and exit (no Slack)")
    args = parser.parse_args(argv)
    configure_stdio()

    try:
        cfg = load(args.config) if args.config else load()
    except ConfigError as exc:
        print(f"listener: config error: {exc}", file=sys.stderr)
        return 1

    if args.once is not None:
        print(handle_slash(args.once, cfg).text)
        return 0

    bot_token = os.environ.get("SLACK_BOT_TOKEN", "")
    app_token = os.environ.get("SLACK_APP_TOKEN", "")
    if not bot_token or not app_token:
        print("listener: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set",
              file=sys.stderr)
        return 1

    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.request import SocketModeRequest
    from slack_sdk.socket_mode.response import SocketModeResponse
    from slack_sdk.web import WebClient

    web = WebClient(token=bot_token)
    client = SocketModeClient(app_token=app_token, web_client=web)

    def on_request(sm_client: SocketModeClient, request: SocketModeRequest) -> None:
        # Acknowledge FIRST: Slack retries anything not acked within 3s, which
        # would run the command twice.
        sm_client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
        try:
            if request.type == "slash_commands":
                payload = request.payload
                reply = handle_slash(payload.get("text", ""), cfg)
                _post_reply(web, payload["channel_id"], reply,
                            payload.get("user_id", ""))

            elif request.type == "interactive":
                payload = request.payload
                actions = payload.get("actions") or []
                if not actions:
                    return
                reply = dispatch_action(cfg, actions[0].get("action_id", ""),
                                        ticket_from_payload(payload))
                channel = (payload.get("channel") or {}).get("id")
                if channel:
                    _post_reply(web, channel, reply)
        except Exception as exc:                      # a listener crash kills the loop
            print(f"listener: error handling {request.type}: {exc}", file=sys.stderr)

    client.socket_mode_request_listeners.append(on_request)
    print("listener: connecting to Slack (Socket Mode)...", file=sys.stderr)
    client.connect()
    from threading import Event
    Event().wait()
    return 0


if __name__ == "__main__":
    sys.exit(main())
