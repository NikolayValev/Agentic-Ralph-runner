"""GitHub REST access for the wrapper (Phase 2).

Deliberately minimal and deliberately incomplete: there is NO merge function and
no deploy function anywhere in this module. Plan sec.4 says the system never
merges and never ships to production; the cheapest way to guarantee that is for
the capability to not exist in the code at all, rather than to exist behind a
flag the agent might talk its way past.

Pushing is NOT done here -- `git push` uses the machine's existing credential
helper, so no token passes through this process for that step.
"""

from __future__ import annotations

import re
from typing import Any

import requests

API = "https://api.github.com"
TIMEOUT = 30

_REMOTE_RE = re.compile(
    r"(?:https://github\.com/|git@github\.com:)(?P<owner>[^/]+)/(?P<repo>[^/.]+)"
)


class GitHubError(RuntimeError):
    """GitHub was unreachable, rejected the token, or refused the request."""


def repo_slug(remote_url: str) -> str:
    """'https://github.com/Owner/name.git' -> 'Owner/name'."""
    match = _REMOTE_RE.search(remote_url or "")
    if not match:
        raise GitHubError(f"could not parse a GitHub owner/repo out of {remote_url!r}")
    return f"{match.group('owner')}/{match.group('repo')}"


def _request(token: str, method: str, path: str, **kwargs: Any) -> Any:
    if not token:
        raise GitHubError(
            "GITHUB_TOKEN is not set. Needed to open the pull request; add it to .env "
            "with scope 'repo' (classic) or contents:write + pull_requests:write."
        )
    try:
        response = requests.request(
            method,
            f"{API}{path}",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=TIMEOUT,
            **kwargs,
        )
    except requests.RequestException as exc:
        raise GitHubError(f"GitHub request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise GitHubError(
            f"GitHub rejected the token (HTTP {response.status_code}): "
            f"{response.json().get('message', '') if response.content else ''}"
        )
    if response.status_code >= 400:
        detail = ""
        try:
            body = response.json()
            detail = body.get("message", "")
            if body.get("errors"):
                detail += f" {body['errors']}"
        except ValueError:
            detail = response.text[:300]
        raise GitHubError(f"GitHub HTTP {response.status_code}: {detail}")
    return response.json() if response.content else {}


def find_pr(token: str, slug: str, head_branch: str) -> str | None:
    """URL of the open PR for this branch, if one already exists."""
    owner = slug.split("/")[0]
    prs = _request(
        token, "GET", f"/repos/{slug}/pulls",
        params={"head": f"{owner}:{head_branch}", "state": "open"},
    )
    return prs[0]["html_url"] if prs else None


def open_pr(
    token: str, slug: str, *, head: str, base: str, title: str, body: str
) -> str:
    """Open a PR and leave it in review. Idempotent: reuses an existing one.

    Never merges. The PR is the handoff point to a human, by design.
    """
    existing = find_pr(token, slug, head)
    if existing:
        return existing
    created = _request(
        token, "POST", f"/repos/{slug}/pulls",
        json={"title": title, "body": body, "head": head, "base": base},
    )
    return created["html_url"]


def close_pr(token: str, slug: str, pr_url: str) -> None:
    """Close a PR without merging. There is no merge counterpart, by design."""
    number = pr_url.rstrip("/").split("/")[-1]
    if not number.isdigit():
        raise GitHubError(f"could not read a PR number from {pr_url!r}")
    _request(token, "PATCH", f"/repos/{slug}/pulls/{number}", json={"state": "closed"})


def delete_branch(token: str, slug: str, branch: str) -> None:
    """Delete a remote branch. Tolerates the branch already being gone."""
    try:
        _request(token, "DELETE", f"/repos/{slug}/git/refs/heads/{branch}")
    except GitHubError as exc:
        if "422" in str(exc) or "not exist" in str(exc).lower() or "404" in str(exc):
            return
        raise
