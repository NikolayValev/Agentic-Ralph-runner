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

# Actions that need a human click before they execute. The line is drawn by
# direction of safety, not by whether Linear gets written. `stop` halts an
# autonomous system -- a misparse there costs a paused loop that one word
# ("go") resumes, so it can act immediately. `go` re-arms the loop and clears
# its failure streak, so a low-confidence misparse could restart work a human
# deliberately halted -- that is fail-dangerous, so it joins the
# ticket-writing actions (bump/skip/unskip) behind a confirmation click.
# list/status read nothing and change nothing, so they are immediate too.
NEEDS_CONFIRMATION = frozenset({"bump", "skip", "unskip", "go"})

FALLBACK = "Try `/ralph help` for the commands I understand."


def needs_confirmation(action: str) -> bool:
    """True when `action` must be rendered as a confirmation with buttons
    rather than executed straight away."""
    return action in NEEDS_CONFIRMATION


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
        endpoint = local["endpoint"]
        model = local["model"]
        num_ctx = int(local["num_ctx"])
        raw = chat(endpoint, model, prompt, num_ctx=num_ctx, schema=INTENT_SCHEMA)
        intent = parse_intent(raw)
    except OllamaError as exc:
        return None, f":warning: my local model is not answering ({exc}). {FALLBACK}"
    except (KeyError, ValueError) as exc:
        return None, (
            f":warning: my local model config is broken ({exc}). {FALLBACK}")

    floor = float(local.get("min_confidence", 0.6))
    # With an empty queue, "start working on the next one" parses as unknown --
    # there is no next one. Replying only "I did not follow that" blames the
    # sentence, which was fine; the queue was the problem. Say which.
    empty = " Nothing is in the queue right now." if not ranked else ""
    if intent.action == "unknown":
        return None, f"I did not follow that.{empty} {FALLBACK}"
    if intent.confidence < floor:
        return None, (
            f"I am not sure enough to act on that (confidence "
            f"{intent.confidence:.0%}).{empty} {FALLBACK}")

    if intent.action == "unskip":
        # A skipped ticket sits in Backlog, not among the eligible/unstarted
        # queue rank_issues returns -- validate it against the labelled
        # issues that are NOT unstarted instead of against `ranked`.
        skipped = {
            i["identifier"] for i in issues if i.get("state_type") != "unstarted"
        }
        if intent.ticket not in skipped:
            listed = ", ".join(sorted(skipped)) or "nothing"
            return None, (
                f"{intent.ticket or 'That ticket'} is not skipped. "
                f"Currently skipped: {listed}.")
    elif intent.action not in TICKETLESS:
        queued = {i["identifier"] for i in ranked}
        if intent.ticket not in queued:
            listed = ", ".join(sorted(queued)) or "nothing"
            return None, (
                f"{intent.ticket or 'That ticket'} is not in the queue. "
                f"Currently queued: {listed}.")

    return intent, ""
