"""Phase 5: what /ralph and the buttons actually do.

Transport-free on purpose. Socket Mode needs a live websocket and two tokens;
these handlers need neither, so the behaviour can be tested directly.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from ralph.breaker import read_count, reset as reset_streak
from ralph.config import STOP_FILE, Config
from ralph.github import GitHubError, close_pr, delete_branch, find_pr, repo_slug
from ralph.linear import LinearError, move_issue

HELP = (
    "*Ralph commands*\n"
    "`/ralph status` - schedule window, run budget, failure streak, STOP state\n"
    "`/ralph stop`   - pause the loop (writes STOP; ticks become no-ops)\n"
    "`/ralph go`     - resume (clears STOP and the failure streak)\n"
    "`/ralph help`   - this message"
)


@dataclass(frozen=True)
class Reply:
    text: str
    ephemeral: bool = True


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


def handle_slash(text: str, cfg: Config) -> Reply:
    command = (text or "").strip().split()[0].lower() if (text or "").strip() else "status"

    if command in ("status", "s"):
        return Reply(status_text(cfg))
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
