"""Phase 5: what /ralph and the buttons actually do.

Transport-free on purpose. Socket Mode needs a live websocket and two tokens;
these handlers need neither, so the behaviour can be tested directly.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime

from ralph.breaker import read_count, reset as reset_streak
from ralph.config import STOP_FILE, Config
from ralph.github import GitHubError, close_pr, delete_branch, find_pr, repo_slug
from ralph.linear import (LinearError, URGENT, fetch_labelled_issues, move_issue,
                          rank_issues, set_priority)
from ralph.slack import build_queue_blocks, queue_headline

HELP = (
    "*Ralph commands*\n"
    "`/ralph status`          - schedule window, run budget, failure streak, STOP state\n"
    "`/ralph list`            - the queue, in the order Ralph will work it\n"
    "`/ralph bump NIK-123`    - make a ticket the next pick (sets Urgent in Linear)\n"
    "`/ralph skip NIK-123`    - park a ticket in Backlog; Ralph ignores it\n"
    "`/ralph unskip NIK-123`  - return a parked ticket to Todo\n"
    "`/ralph stop`            - pause the loop (writes STOP; ticks become no-ops)\n"
    "`/ralph go`              - resume (clears STOP and the failure streak)\n"
    "`/ralph help`            - this message"
)


@dataclass(frozen=True)
class Reply:
    text: str
    ephemeral: bool = True
    # Block Kit payload for replies that are richer than a line of text. The
    # transport falls back to `text` when this is None.
    blocks: list[dict] | None = None


def status_text(cfg: Config, *, now: datetime | None = None) -> str:
    now = now or datetime.now()
    stopped = STOP_FILE.exists()
    reason = ""
    if stopped:
        try:
            reason = STOP_FILE.read_text(encoding="utf-8").strip()
        except OSError:
            reason = "(unreadable)"

    threshold = int(cfg.raw["safety"].get("circuit_breaker_threshold", 3))
    lines = [
        f"*Ralph* - {'PAUSED' if stopped else 'running'}",
        f"- repo: `{cfg.repo['name']}`  model: `{cfg.loop['model']}`",
        f"- window: {', '.join(str(w) for w in cfg.windows)} "
        f"(now {now:%H:%M}, {'inside' if cfg.in_window(now) else 'outside'})",
        f"- run budget today: {cfg.runs_today()}/{cfg.max_runs_per_day}",
        f"- failure streak: {read_count()}/{threshold}",
    ]
    if stopped:
        lines.append(f"- STOP: {reason}")
    return "\n".join(lines)


TICKET_RE = re.compile(r"^[A-Za-z]+-\d+$")


def normalize_ticket(raw: str, *, team_key: str = "") -> str:
    """Uppercase a ticket id, or return '' if it is not one.

    Slack helpfully wraps bare text in angle brackets when it thinks it is a
    link, so those are stripped before matching. When Slack renders a link as
    `<url|label>`, splitting on '|' keeps the URL half, not the label -- that
    is deliberate: the URL will not match TICKET_RE, so the input fails closed
    into the usage message rather than risking a match on the wrong text.

    When `team_key` is given, the ticket's prefix must equal it
    (case-insensitively). LINEAR_API_KEY is workspace-wide and neither
    `set_priority` nor `move_issue` scopes its write to a team, so this is the
    only thing standing between a typo like `ENG-99` and a real mutation on a
    ticket that has nothing to do with this team's queue.
    """
    candidate = (raw or "").strip().strip("<>").split("|")[0].strip().upper()
    if not TICKET_RE.match(candidate):
        return ""
    if team_key and not candidate.startswith(f"{team_key.upper()}-"):
        return ""
    return candidate


def _api_key() -> str:
    return os.environ.get("LINEAR_API_KEY", "")


def _fetch_ranked_queue(cfg: Config) -> tuple[list[dict], list[str], Reply | None]:
    """Fetch and rank the queue, exactly the way `handle_list` does.

    Shared by `handle_list`, `handle_bump`, and `handle_skip` so all three agree
    on what "in the queue" means. Returns (ranked, skipped, None) on success, or
    ([], [], Reply) if the fetch or a malformed node blew up. A malformed Linear
    node raises ValueError or KeyError (bad priority, missing id/identifier),
    not LinearError, so both must be caught or the caller gets no reply at all.
    """
    try:
        issues = fetch_labelled_issues(
            _api_key(), cfg.linear["team_key"], cfg.linear["eligible_label"])
        ranked, skipped = rank_issues(
            issues,
            eligible_label=cfg.linear["eligible_label"],
            repo_label=cfg.linear.get("repo_label", ""))
    except (LinearError, ValueError, KeyError) as exc:
        return [], [], Reply(f":warning: could not read the queue: {exc}")
    return ranked, skipped, None


def handle_list(cfg: Config) -> Reply:
    """Show the queue in the order the gate will work it."""
    ranked, skipped, err = _fetch_ranked_queue(cfg)
    if err:
        return err
    return Reply(queue_headline(ranked), blocks=build_queue_blocks(ranked, skipped))


def _require_queued(cfg: Config, ticket: str) -> Reply | None:
    """Confirm `ticket` is in the ranked, unstarted queue before a write.

    Returns None when it is safe to proceed, or a Reply refusing the action
    when it is not. No write happens in the refusal path.

    An in-flight ticket is In Progress, which is not one of ELIGIBLE_STATE_TYPES,
    so it is absent from `ranked` exactly like a Done or nonexistent ticket --
    this is what stops `/ralph skip` from "parking" a ticket the 1am run is
    already holding open, only to have `finalize.py` unconditionally move it to
    In Review when the run ends and silently undo the human's skip. The reply
    points at `/ralph stop` because that is the actual way to halt work already
    in progress; skip/bump only affect what the gate has not started yet.
    """
    ranked, _skipped, err = _fetch_ranked_queue(cfg)
    if err:
        return err
    if any(issue["identifier"] == ticket for issue in ranked):
        return None
    identifiers = ", ".join(issue["identifier"] for issue in ranked)
    in_queue = f"In the queue: {identifiers}." if identifiers else "The queue is empty."
    return Reply(
        f":warning: {ticket} is not in the queue -- nothing changed. "
        f"{in_queue} If {ticket} is already being worked, `/ralph stop` halts "
        f"the run instead.",
        ephemeral=False)


def handle_bump(cfg: Config, ticket: str) -> Reply:
    """Make a ticket the next pick by setting it Urgent in Linear."""
    ticket = normalize_ticket(ticket, team_key=cfg.linear["team_key"])
    if not ticket:
        return Reply("Usage: `/ralph bump NIK-123`")
    refusal = _require_queued(cfg, ticket)
    if refusal:
        return refusal
    try:
        set_priority(_api_key(), ticket, URGENT)
    except LinearError as exc:
        return Reply(f":warning: {ticket}: could not bump: {exc}", ephemeral=False)
    return Reply(
        f":arrow_up: {ticket} set to Urgent. It goes next unless something else "
        f"is also Urgent and older.",
        ephemeral=False)


def _move(cfg: Config, ticket: str, state_key: str, default: str,
          icon: str, verb: str, usage: str, *, require_queued: bool = False) -> Reply:
    """Shared body for skip and unskip: both are one move_issue call.

    `require_queued` gates the pre-write queue-membership check. Only skip uses
    it: a parked ticket lives in Backlog, which is by definition absent from the
    unstarted queue, so applying the same check to unskip would make unskip
    permanently impossible.
    """
    ticket = normalize_ticket(ticket, team_key=cfg.linear["team_key"])
    if not ticket:
        return Reply(usage)
    if require_queued:
        refusal = _require_queued(cfg, ticket)
        if refusal:
            return refusal
    state = cfg.linear.get(state_key, default)
    try:
        landed = move_issue(_api_key(), ticket, state, cfg.linear["team_key"])
    except LinearError as exc:
        return Reply(f":warning: {ticket}: could not {verb}: {exc}", ephemeral=False)
    return Reply(f"{icon} {ticket} moved to {landed}.", ephemeral=False)


def handle_skip(cfg: Config, ticket: str) -> Reply:
    """Park a ticket: Backlog is not an eligible state, so the gate ignores it."""
    return _move(cfg, ticket, "backlog_state", "Backlog",
                 ":double_vertical_bar:", "skip", "Usage: `/ralph skip NIK-123`",
                 require_queued=True)


def handle_unskip(cfg: Config, ticket: str) -> Reply:
    """Return a parked ticket to the queue."""
    return _move(cfg, ticket, "todo_state", "Todo",
                 ":leftwards_arrow_with_hook:", "unskip",
                 "Usage: `/ralph unskip NIK-123`")


def handle_slash(text: str, cfg: Config) -> Reply:
    parts = (text or "").strip().split()
    command = parts[0].lower() if parts else "status"
    argument = parts[1] if len(parts) > 1 else ""

    if command in ("status", "s"):
        return Reply(status_text(cfg))
    if command in ("list", "queue", "q"):
        return handle_list(cfg)
    if command in ("bump", "next"):
        return handle_bump(cfg, argument)
    if command == "skip":
        return handle_skip(cfg, argument)
    if command == "unskip":
        return handle_unskip(cfg, argument)
    if command == "stop":
        STOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        STOP_FILE.write_text(
            f"stopped via /ralph stop at {datetime.now():%Y-%m-%d %H:%M:%S}\n",
            encoding="utf-8")
        return Reply(":octagonal_sign: Ralph paused. Ticks will no-op until `/ralph go`.",
                     ephemeral=False)
    if command == "go":
        was_stopped = STOP_FILE.exists()
        if was_stopped:
            STOP_FILE.unlink()
        reset_streak()
        return Reply(
            ":white_check_mark: Ralph resumed; failure streak cleared."
            if was_stopped else ":white_check_mark: Ralph was already running; streak cleared.",
            ephemeral=False)
    if command in ("help", "h", "?"):
        return Reply(HELP)
    return Reply(f"Unknown command `{command}`.\n\n{HELP}")


def handle_approve(cfg: Config, ticket: str) -> Reply:
    """MVP stub, per plan sec.11: frees the slot, the human merges.

    Deliberately does NOT merge. The button opening the PR in the browser is the
    whole interaction; merging stays a human action in GitHub, which keeps
    "never merges" true of the system rather than merely configured.
    """
    return Reply(
        f":white_check_mark: {ticket} left for you to merge in GitHub. "
        f"Ralph does not merge; the PR stays open until you do.",
        ephemeral=False)


def handle_discard(cfg: Config, ticket: str) -> Reply:
    """Close the PR, delete the branch, return the ticket to Todo."""
    token = os.environ.get("GITHUB_TOKEN", "")
    branch = cfg.branch_for(ticket)
    steps = []
    try:
        slug = repo_slug(cfg.repo["remote"])
        pr = find_pr(token, slug, branch)
        if pr:
            close_pr(token, slug, pr)
            steps.append(f"closed <{pr}|PR>")
        delete_branch(token, slug, branch)
        steps.append(f"deleted `{branch}`")
    except GitHubError as exc:
        return Reply(f":warning: {ticket}: could not discard: {exc}", ephemeral=False)

    try:
        state = move_issue(os.environ.get("LINEAR_API_KEY", ""), ticket,
                           cfg.linear["todo_state"], cfg.linear["team_key"])
        steps.append(f"moved to {state}")
    except LinearError as exc:
        steps.append(f"could not move ticket ({exc})")

    return Reply(f":wastebasket: {ticket}: " + ", ".join(steps) + ".", ephemeral=False)
