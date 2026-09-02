"""Slack message construction (Phase 4).

Building the message is pure and separate from sending it, so the payload can be
tested exhaustively without a token -- which matters because a malformed Block
Kit payload is rejected at send time, in an unattended run, with nobody watching.
"""

from __future__ import annotations

from ralph.contracts import AgentReport

# Statuses that produce no Slack message at all. A quiet tick must stay quiet:
# an unattended loop that pings every time it finds nothing trains you to ignore it.
SILENT_STATUSES = ("nothing_eligible",)

STATUS_STYLE = {
    "in_review": (":large_green_circle:", "Ready for review"),
    "blocked":   (":large_yellow_circle:", "Blocked"),
    "error":     (":red_circle:", "Error"),
}

ACTION_APPROVE = "ralph_approve"
ACTION_DISCARD = "ralph_discard"
ACTION_BUMP = "ralph_bump"
ACTION_SKIP = "ralph_skip"

# Slack caps a message at 50 blocks and each row costs two. Ten rows is plenty
# to choose from and leaves headroom for the header and context lines.
QUEUE_ROW_LIMIT = 10

PRIORITY_LABEL = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}


def should_notify(report: AgentReport) -> bool:
    return report.status not in SILENT_STATUSES


def linear_url(ticket: str) -> str:
    return f"https://linear.app/nikolayvalev/issue/{ticket}"


def build_message(report: AgentReport, *, channel: str) -> dict:
    """A Block Kit payload for chat.postMessage.

    `text` is set as well as `blocks`: it is the notification/fallback string,
    and without it the push notification is blank.
    """
    icon, label = STATUS_STYLE.get(report.status, (":white_circle:", report.status))
    headline = f"{icon} {report.ticket} - {label}"

    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{headline}*\n{report.summary}"}},
    ]

    links = [f"<{linear_url(report.ticket)}|Linear>"]
    if report.pr_url:
        links.append(f"<{report.pr_url}|Pull request>")
    if report.preview_url:
        links.append(f"<{report.preview_url}|Preview>")
    else:
        links.append("_no preview_")
    if report.branch:
        links.append(f"`{report.branch}`")
    blocks.append({"type": "context",
                   "elements": [{"type": "mrkdwn", "text": "  |  ".join(links)}]})

    # Buttons only where there is something to act on. A blocked run has no PR,
    # so offering "Approve" would be meaningless.
    if report.status == "in_review" and report.pr_url:
        blocks.append({
            "type": "actions",
            "block_id": f"ralph_actions::{report.ticket}",
            "elements": [
                {"type": "button", "action_id": ACTION_APPROVE,
                 "text": {"type": "plain_text", "text": "Approve"},
                 "style": "primary", "value": report.ticket,
                 "url": report.pr_url},
                {"type": "button", "action_id": ACTION_DISCARD,
                 "text": {"type": "plain_text", "text": "Discard"},
                 "style": "danger", "value": report.ticket,
                 "confirm": {
                     "title": {"type": "plain_text", "text": "Discard this run?"},
                     "text": {"type": "mrkdwn",
                              "text": f"Deletes branch `{report.branch}` and returns "
                                      f"{report.ticket} to Todo. The PR is closed, not merged."},
                     "confirm": {"type": "plain_text", "text": "Discard"},
                     "deny": {"type": "plain_text", "text": "Keep"}}},
            ],
        })

    return {
        "channel": channel,
        "text": f"{headline}: {report.summary}"[:3000],
        "blocks": blocks,
        "unfurl_links": False,
        "unfurl_media": False,
    }


def escape_mrkdwn(text: str) -> str:
    """Escape Slack mrkdwn special characters: & < >.

    Must escape & first to avoid double-escaping ampersands introduced by < and >.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def queue_headline(ranked: list[dict]) -> str:
    """One-line summary, also used as the notification fallback text."""
    if not ranked:
        return ":white_circle: Ralph queue - nothing eligible"
    return (f":clipboard: Ralph queue - {len(ranked)} eligible, "
            f"next is {ranked[0]['identifier']}")


def build_queue_blocks(ranked: list[dict], skipped: list[str]) -> list[dict]:
    """Block Kit for `/ralph list`: the pick order, with per-row controls.

    Takes plain issue dicts rather than importing from ralph.linear, so message
    construction stays independent of how the queue was produced.
    """
    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{queue_headline(ranked)}*"}},
    ]

    for position, issue in enumerate(ranked[:QUEUE_ROW_LIMIT], start=1):
        ticket = issue["identifier"]
        marker = ":arrow_forward:" if position == 1 else f"{position}."
        priority = PRIORITY_LABEL.get(int(issue.get("priority") or 0), "None")
        title = (issue.get("title") or "")[:80]
        escaped_title = escape_mrkdwn(title)
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"{marker}  <{linear_url(ticket)}|{ticket}>  {escaped_title}  _{priority}_"},
        })
        blocks.append({
            "type": "actions",
            "block_id": f"ralph_queue::{ticket}",
            "elements": [
                # Neither button is styled `danger`: bump and skip are both
                # reversible, unlike Discard on a run report.
                {"type": "button", "action_id": ACTION_BUMP,
                 "text": {"type": "plain_text", "text": "Bump"}, "value": ticket},
                {"type": "button", "action_id": ACTION_SKIP,
                 "text": {"type": "plain_text", "text": "Skip"}, "value": ticket},
            ],
        })

    hidden = len(ranked) - QUEUE_ROW_LIMIT
    if hidden > 0:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"_...and {hidden} more_"}]})

    if skipped:
        # Skip reasons embed ticket titles and Linear state names (see
        # rank_issues), which carry the same mrkdwn injection risk as the
        # titles rendered above -- so they get the same escaping.
        shown = "  |  ".join(escape_mrkdwn(reason) for reason in skipped[:5])
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"_not queued: {shown}_"[:3000]}]})

    return blocks
