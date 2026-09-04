"""Phase 2: turn a sentence into one of the actions the bot already performs.

This module decides WHAT to propose. It never writes to Linear -- executing a
proposal is the confirmation step's job, which routes into the same handlers
the buttons use. The NL layer therefore has no privileges of its own: the
worst a misparse can do is offer the wrong button.
"""

from __future__ import annotations

import os

from ralph.config import Config
from ralph.linear import LinearError, fetch_labelled_issues, rank_issues
from ralph.ollama import (INTENT_PROMPT, INTENT_SCHEMA, Intent, OllamaError,
                          chat, parse_intent)

# Actions that read nothing from the queue and so need no ticket id.
TICKETLESS = ("list", "status", "stop", "go")

FALLBACK = "Try `/ralph help` for the commands I understand."


def format_queue(ranked: list[dict]) -> str:
    """The queue as the model sees it. Ids and titles only -- it matches
    'the drizzle one' against titles, and copies the id verbatim."""
    if not ranked:
        return "(the queue is empty)"
    return "\n".join(
        f"- {i['identifier']}: {(i.get('title') or '')[:80]}" for i in ranked)


def interpret(cfg: Config, message: str) -> tuple[Intent | None, str]:
    """Return (intent, "") when confident, or (None, question) when not."""
    try:
        issues = fetch_labelled_issues(
            os.environ.get("LINEAR_API_KEY", ""),
            cfg.linear["team_key"], cfg.linear["eligible_label"])
        ranked, _ = rank_issues(
            issues,
            eligible_label=cfg.linear["eligible_label"],
            repo_label=cfg.linear.get("repo_label", ""))
    except (LinearError, ValueError, KeyError) as exc:
        return None, f":warning: I could not read the queue: {exc}"

    local = cfg.local
    prompt = INTENT_PROMPT.format(
        queue=format_queue(ranked), message=message.strip()[:500])
    try:
        raw = chat(local["endpoint"], local["model"], prompt,
                   num_ctx=int(local["num_ctx"]), schema=INTENT_SCHEMA)
        intent = parse_intent(raw)
    except OllamaError as exc:
        return None, f":warning: my local model is not answering ({exc}). {FALLBACK}"

    floor = float(local.get("min_confidence", 0.6))
    if intent.action == "unknown":
        return None, f"I did not follow that. {FALLBACK}"
    if intent.confidence < floor:
        return None, (
            f"I am not sure enough to act on that (confidence "
            f"{intent.confidence:.0%}). {FALLBACK}")

    if intent.action not in TICKETLESS:
        queued = {i["identifier"] for i in ranked}
        if intent.ticket not in queued:
            listed = ", ".join(sorted(queued)) or "nothing"
            return None, (
                f"{intent.ticket or 'That ticket'} is not in the queue. "
                f"Currently queued: {listed}.")

    return intent, ""
