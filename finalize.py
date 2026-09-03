#!/usr/bin/env python
"""Everything after the agent returns (Phase 2).

Deliberately outside the model: verifying what changed, refusing forbidden
edits, pushing, opening the PR, and moving the ticket. A confused agent cannot
skip these, and cannot do them wrongly, because it never had the capability.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from ralph.config import ConfigError, configure_stdio, forbidden_hits, load
from ralph.contracts import AgentReport, ContractError, parse_report_from_output
from ralph.github import GitHubError, open_pr, repo_slug
from ralph.linear import LinearError, add_comment, move_issue, remove_label
from ralph.breaker import record as record_outcome
from ralph.vercel import VercelError, find_preview, protection_of


class FinalizeError(RuntimeError):
    pass


def git(*args: str, cwd: Path, check: bool = True) -> str:
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                          text=True, timeout=600)
    if check and proc.returncode != 0:
        raise FinalizeError(f"git {' '.join(args)} failed: {proc.stderr.strip()[:300]}")
    return proc.stdout.strip()


def changed_paths(repo: Path, base: str) -> list[str]:
    """Files this branch changes relative to the base it was cut from."""
    out = git("diff", "--name-only", f"origin/{base}...HEAD", cwd=repo)
    return [line.strip() for line in out.splitlines() if line.strip()]


def commits_ahead(repo: Path, base: str) -> int:
    return int(git("rev-list", "--count", f"origin/{base}..HEAD", cwd=repo) or 0)


def build_report(raw_output: str, *, ticket: str, branch: str) -> AgentReport:
    """Parse the agent's report, then overwrite the fields it must not own."""
    try:
        report = parse_report_from_output(raw_output)
    except ContractError:
        # A missing/invalid report is itself a result worth reporting, not a crash.
        return AgentReport(
            status="error", mode="tagged", ticket=ticket, branch=branch,
            pr_url="", preview_url="",
            summary="agent produced no valid report JSON; see the run log",
        )
    # The agent does not get to assert its own ticket/branch.
    return AgentReport(
        status=report.status, mode="tagged", ticket=ticket, branch=branch,
        pr_url="", preview_url="", summary=report.summary[:240],
    )


def note_outcome(cfg, status: str) -> None:
    """Feed the circuit breaker. Called for every terminal outcome."""
    threshold = int(cfg.raw["safety"].get("circuit_breaker_threshold", 3))
    result = record_outcome(status, threshold)
    print(f"finalize: breaker {result.reason}", file=sys.stderr)
    if result.tripped:
        print("finalize: STOP written; the loop is now paused", file=sys.stderr)


def deploy_id(cfg, env_name: str, config_key: str) -> str:
    """Vercel ids: environment first, config.yaml as fallback.

    These identify a project, they are not credentials -- acting on them still
    needs VERCEL_TOKEN. But they are account infrastructure and this repo is
    public, so .env is their home and config.yaml keeps the key documented and
    empty. preflight already requires VERCEL_PROJECT_ID in the environment.
    """
    return os.environ.get(env_name, "") or cfg.deploy.get(config_key, "")


def resolve_preview(cfg, branch: str) -> tuple[str, str]:
    """(preview_url, note). Never returns a URL that is reachable anonymously.

    A push has only just happened, so the deployment may still be building; we
    wait briefly rather than reporting an empty URL for a preview that is about
    to exist. If the URL turns out to be public we deliberately withhold it:
    handing out an open link to unreviewed work is worse than reporting none,
    and plan sec.3 requires staging to be protected.
    """
    token = os.environ.get("VERCEL_TOKEN", "")
    if not token:
        return "", "VERCEL_TOKEN unset; preview not looked up"
    try:
        preview = find_preview(
            token,
            deploy_id(cfg, "VERCEL_PROJECT_ID", "vercel_project_id"),
            deploy_id(cfg, "VERCEL_ORG_ID", "vercel_org_id"),
            branch,
            wait_seconds=int(cfg.deploy.get("preview_wait_seconds", 180)),
        )
    except VercelError as exc:
        return "", f"preview lookup failed: {exc}"
    if preview is None:
        return "", "no preview deployment found for this branch"

    guard = protection_of(preview.url)
    if not guard.protected:
        return "", (
            f"WITHHELD: preview {preview.url} is {guard.detail}; "
            "staging must not be publicly reachable"
        )
    note = f"preview {preview.state}, {guard.detail}"
    return (preview.url if preview.url.startswith("http")
            else f"https://{preview.url}"), note


def dequeue(cfg, ticket: str, status: str, reason: str) -> None:
    """Hand a non-productive run back to a human, and stop the loop retrying it.

    Two failure modes this avoids:
      - Leaving the ticket in In Progress. The gate only selects unstarted
        tickets, so it would never be retried and the board would claim someone
        is working it.
      - Moving it back to Todo and nothing else. It would be re-selected on the
        very next tick and spend another Claude turn reaching the same
        conclusion, indefinitely.

    So: explain on the ticket, drop the opt-in label, and return it to Todo.
    Re-queuing is then a deliberate human act.
    """
    key = os.environ.get("LINEAR_API_KEY", "")
    label = cfg.linear["eligible_label"]
    try:
        add_comment(
            key, ticket,
            f"**Ralph run {status}.**\n\n{reason}\n\n"
            f"The `{label}` label has been removed so the agent will not "
            f"retry this automatically. Re-add it to queue the ticket again.",
        )
        removed = remove_label(key, ticket, label)
        state = move_issue(key, ticket, cfg.linear["todo_state"], cfg.linear["team_key"])
        print(f"finalize: de-queued {ticket} (label removed: {removed}, state: {state})",
              file=sys.stderr)
    except LinearError as exc:
        print(f"finalize: WARNING could not de-queue {ticket}: {exc}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Post-run: verify, push, PR, transition")
    parser.add_argument("--ticket", required=True)
    parser.add_argument("--output", required=True, help="file holding the agent's stdout")
    parser.add_argument("--title", default="")
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true",
                        help="verify and report, but do not push, PR, or transition")
    args = parser.parse_args(argv)
    configure_stdio()

    try:
        cfg = load(args.config) if args.config else load()
    except ConfigError as exc:
        print(f"finalize: config error: {exc}", file=sys.stderr)
        return 1

    repo = Path(os.environ.get("REPO_DIR", ""))
    if not repo.exists():
        print(f"finalize: REPO_DIR does not exist: {repo}", file=sys.stderr)
        return 1

    branch = cfg.branch_for(args.ticket)
    base = cfg.repo["default_branch"]
    raw_output = Path(args.output).read_text(encoding="utf-8", errors="replace")
    report = build_report(raw_output, ticket=args.ticket, branch=branch)

    # --- refuse forbidden edits, whatever the agent claims ----------------
    changed = changed_paths(repo, base)
    hits = forbidden_hits(changed, cfg.raw["safety"]["forbidden_paths"])
    if hits:
        report = AgentReport(
            status="blocked", mode="tagged", ticket=args.ticket, branch=branch,
            pr_url="", preview_url="",
            summary=f"run rejected: modified forbidden path(s) {', '.join(hits)[:150]}",
        )
        print(report.to_json())
        return 1

    ahead = commits_ahead(repo, base)
    if report.status == "in_review" and ahead == 0:
        report = AgentReport(
            status="blocked", mode="tagged", ticket=args.ticket, branch=branch,
            pr_url="", preview_url="",
            summary="agent reported in_review but committed nothing",
        )

    if report.status != "in_review" or args.dry_run:
        if args.dry_run:
            print(f"finalize: dry run; {ahead} commit(s), {len(changed)} file(s) changed",
                  file=sys.stderr)
        else:
            dequeue(cfg, args.ticket, report.status, report.summary)
            note_outcome(cfg, report.status)
        print(report.to_json())
        return 0 if report.status in ("in_review", "blocked") else 1

    # --- push, PR, transition --------------------------------------------
    # git push uses the machine's credential helper; no token passes through here.
    try:
        git("push", "--set-upstream", "origin", branch, "--force-with-lease", cwd=repo)
    except FinalizeError as exc:
        print(f"finalize: {exc}", file=sys.stderr)
        return 1

    try:
        slug = repo_slug(cfg.repo["remote"])
        body = (
            f"Automated increment for [{args.ticket}]"
            f"(https://linear.app/nikolayvalev/issue/{args.ticket})\n\n"
            f"{report.summary}\n\n"
            f"---\nOpened by the Ralph agent and **left in review**. "
            f"It does not merge or deploy to production.\n"
            f"Files changed: {len(changed)}. Commits: {ahead}."
        )
        pr_url = open_pr(
            os.environ.get("GITHUB_TOKEN", ""), slug,
            head=branch, base=base,
            title=f"{args.ticket}: {args.title or report.summary[:60]}",
            body=body,
        )
    except GitHubError as exc:
        print(f"finalize: PR not opened: {exc}", file=sys.stderr)
        print(AgentReport(
            status="blocked", mode="tagged", ticket=args.ticket, branch=branch,
            pr_url="", preview_url="",
            summary=f"branch pushed but PR failed: {str(exc)[:150]}",
        ).to_json())
        return 1

    try:
        state = move_issue(
            os.environ.get("LINEAR_API_KEY", ""), args.ticket,
            cfg.linear["review_state"], cfg.linear["team_key"],
        )
        print(f"finalize: {args.ticket} moved to {state}", file=sys.stderr)
    except LinearError as exc:
        print(f"finalize: WARNING could not move ticket: {exc}", file=sys.stderr)

    preview_url, note = resolve_preview(cfg, branch)
    print(f"finalize: {note}", file=sys.stderr)
    note_outcome(cfg, "in_review")

    print(AgentReport(
        status="in_review", mode="tagged", ticket=args.ticket, branch=branch,
        pr_url=pr_url, preview_url=preview_url, summary=report.summary,
    ).to_json())
    return 0


if __name__ == "__main__":
    sys.exit(main())
