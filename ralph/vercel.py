"""Vercel access for Phase 3: find the branch's preview and prove it is not public.

Two separate questions, deliberately answered separately:

  find_preview()   -- needs VERCEL_TOKEN; asks the API which deployment belongs
                      to this branch.
  protection_of()  -- needs no credentials at all; fetches the URL as an
                      anonymous visitor would and reports what actually happens.

The second matters more. The project setting can say ssoProtection is enabled
while a particular URL is still reachable; the only trustworthy check is to be
the anonymous visitor. Verified signature: 302 to vercel.com/sso-api plus a
_vercel_sso_nonce cookie.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

API = "https://api.vercel.com"
TIMEOUT = 30
SSO_HOST = "vercel.com/sso-api"


class VercelError(RuntimeError):
    pass


@dataclass(frozen=True)
class Preview:
    url: str            # stable branch alias when available, else the deployment URL
    deployment_url: str
    state: str          # READY | BUILDING | ERROR | QUEUED ...
    ready: bool


@dataclass(frozen=True)
class Protection:
    protected: bool
    detail: str


def protection_of(url: str, *, timeout: int = 20) -> Protection:
    """Fetch as an anonymous visitor and report whether Vercel challenged us.

    No token. This is the check that actually answers "is it public".
    """
    if not url:
        return Protection(False, "no URL to check")
    if not url.startswith("http"):
        url = f"https://{url}"
    try:
        response = requests.get(url, allow_redirects=False, timeout=timeout)
    except requests.RequestException as exc:
        return Protection(False, f"could not reach {url}: {exc}")

    location = response.headers.get("location", "")
    cookies = response.headers.get("set-cookie", "")
    if response.status_code in (301, 302, 307, 308) and SSO_HOST in location:
        return Protection(True, "Vercel Authentication (redirects to sso-api)")
    if "_vercel_sso_nonce" in cookies:
        return Protection(True, "Vercel Authentication (sso nonce issued)")
    if response.status_code == 401:
        return Protection(True, f"HTTP 401 ({response.reason})")
    return Protection(
        False,
        f"reachable anonymously: HTTP {response.status_code}"
        + (f" -> {location}" if location else ""),
    )


def _request(token: str, path: str, params: dict) -> dict:
    if not token:
        raise VercelError("VERCEL_TOKEN is not set")
    try:
        response = requests.get(
            f"{API}{path}", params=params,
            headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise VercelError(f"Vercel request failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise VercelError(f"Vercel rejected the token (HTTP {response.status_code})")
    if response.status_code != 200:
        raise VercelError(f"Vercel HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def select_preview(deployments: list[dict], branch: str) -> Preview | None:
    """Newest non-production deployment for `branch`.

    Pure, so the selection rules are testable without a token. Production
    deployments carry target == "production"; previews carry null.
    """
    matches = [
        d for d in deployments
        if (d.get("meta") or {}).get("githubCommitRef") == branch
        and d.get("target") != "production"
    ]
    if not matches:
        return None
    newest = max(matches, key=lambda d: d.get("created") or 0)
    meta = newest.get("meta") or {}
    state = newest.get("state") or "UNKNOWN"
    return Preview(
        url=meta.get("branchAlias") or newest.get("url", ""),
        deployment_url=newest.get("url", ""),
        state=state,
        ready=state == "READY",
    )


def find_preview(
    token: str, project_id: str, team_id: str, branch: str,
    *, wait_seconds: int = 0, poll_every: int = 15,
) -> Preview | None:
    """The branch's preview deployment, optionally waiting for it to appear.

    A push triggers a build, so immediately after `git push` the deployment may
    not exist yet or may still be BUILDING. Callers that want a usable URL pass
    wait_seconds; callers that just want a snapshot pass 0.
    """
    deadline = time.monotonic() + max(wait_seconds, 0)
    while True:
        payload = _request(token, "/v6/deployments",
                           {"projectId": project_id, "teamId": team_id, "limit": 40})
        preview = select_preview(payload.get("deployments", []), branch)
        if preview and preview.ready:
            return preview
        if time.monotonic() >= deadline:
            return preview
        time.sleep(poll_every)
