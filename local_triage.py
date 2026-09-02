#!/usr/bin/env python
"""Phase 6: the Tier-1 pre-filter.

Reads a Task JSON on stdin, emits a PreFilter JSON on stdout (contract sec.7):
    {"run": true, "tier": "claude", "reason": "...", "commit_hint": "..."}

Deciding *not* to spend a Claude turn is the whole point, so the failure
behaviour matters: if the local model is unavailable or unsure, this proceeds to
Claude rather than silently dropping a ticket a human deliberately queued.
Saving a turn must never cost a ticket.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from ralph.config import ConfigError, configure_stdio, load
from ralph.contracts import PreFilter, Task
from ralph.linear import LinearError, _post
from ralph.ollama import (VERDICT_SCHEMA, OllamaError, PROMPT, chat,
                          parse_verdict)

ISSUE_QUERY = """query I($id: String!) { issue(id: $id) { identifier title description } }"""


def proceed(reason: str) -> PreFilter:
    """Fail open: a queued ticket still reaches Claude."""
    return PreFilter(run=True, tier="claude", reason=reason, commit_hint="")


def triage(cfg, ticket: str, title: str, description: str) -> PreFilter:
    local = cfg.local
    if not local.get("enabled"):
        return proceed("local tier disabled; routing to Claude")

    prompt = PROMPT.format(ticket=ticket, title=title, description=description[:6000])
    try:
        raw = chat(local["endpoint"], local["model"], prompt,
                   num_ctx=int(local.get("num_ctx", 16384)),
                   schema=VERDICT_SCHEMA)
        verdict = parse_verdict(raw)
    except OllamaError as exc:
        return proceed(f"local triage unavailable ({exc}); routing to Claude")

    minimum = float(local.get("min_confidence", 0.6))
    if verdict.confidence < minimum:
        return proceed(
            f"local confidence {verdict.confidence:.2f} < {minimum}; routing to Claude")

    # A low-confidence *skip* is the dangerous one; a confident skip is honoured.
    return PreFilter(run=verdict.run, tier=verdict.tier,
                     reason=verdict.reason or "local triage",
                     commit_hint=verdict.commit_hint)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local go/no-go pre-filter")
    parser.add_argument("--config")
    parser.add_argument("--ticket", help="triage this ticket instead of reading stdin")
    parser.add_argument("--title", default="", help="offline: supply the title")
    parser.add_argument("--description", default="", help="offline: supply the body")
    args = parser.parse_args(argv)
    configure_stdio()

    try:
        cfg = load(args.config) if args.config else load()
    except ConfigError as exc:
        print(f"triage: config error: {exc}", file=sys.stderr)
        return 1

    if args.ticket:
        ticket, title, description = args.ticket, args.title, args.description
        if not title and not description:
            try:
                issue = _post(os.environ.get("LINEAR_API_KEY", ""),
                              ISSUE_QUERY, {"id": ticket})["issue"]
                title, description = issue["title"] or "", issue.get("description") or ""
            except LinearError as exc:
                print(f"triage: could not read {ticket}: {exc}", file=sys.stderr)
                print(proceed("ticket unreadable; routing to Claude").to_json())
                return 0
    else:
        try:
            task = Task.parse(json.loads(sys.stdin.read()))
        except Exception as exc:
            print(f"triage: bad task on stdin: {exc}", file=sys.stderr)
            return 1
        ticket = task.ref
        try:
            issue = _post(os.environ.get("LINEAR_API_KEY", ""),
                          ISSUE_QUERY, {"id": ticket})["issue"]
            title, description = issue["title"] or "", issue.get("description") or ""
        except LinearError as exc:
            print(f"triage: could not read {ticket}: {exc}", file=sys.stderr)
            print(proceed("ticket unreadable; routing to Claude").to_json())
            return 0

    result = triage(cfg, ticket, title, description)
    print(f"triage: {ticket} -> run={result.run} tier={result.tier} ({result.reason})",
          file=sys.stderr)
    print(result.to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
