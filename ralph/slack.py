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
