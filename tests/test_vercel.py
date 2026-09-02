"""Phase 3: the right deployment, and never a publicly reachable one."""

from __future__ import annotations

import pytest

from ralph.vercel import Preview, Protection, protection_of, select_preview

BRANCH = "ralph/NIK-104"


def deployment(ref, *, target=None, state="READY", created=1000, alias="alias.vercel.app",
               url="dpl.vercel.app"):
    return {"url": url, "state": state, "target": target, "created": created,
            "meta": {"githubCommitRef": ref, "branchAlias": alias}}


def test_selects_the_branchs_preview():
    got = select_preview([deployment("main", target="production"),
                          deployment(BRANCH)], BRANCH)
    assert got is not None and got.ready


def test_ignores_production_deployments():
    """Production must never be reported as the staging URL (plan sec.4)."""
    only_prod = [deployment(BRANCH, target="production")]
    assert select_preview(only_prod, BRANCH) is None


def test_ignores_other_branches():
    assert select_preview([deployment("other/branch")], BRANCH) is None


def test_picks_the_newest():
    got = select_preview([
        deployment(BRANCH, created=100, alias="old.vercel.app"),
        deployment(BRANCH, created=900, alias="new.vercel.app"),
        deployment(BRANCH, created=500, alias="mid.vercel.app"),
    ], BRANCH)
    assert got.url == "new.vercel.app"


def test_prefers_the_stable_branch_alias():
    """The alias survives redeploys; the deployment URL is per-build."""
    got = select_preview([deployment(BRANCH, alias="stable.vercel.app",
                                     url="build-xyz.vercel.app")], BRANCH)
    assert got.url == "stable.vercel.app"
    assert got.deployment_url == "build-xyz.vercel.app"


def test_falls_back_to_deployment_url_without_alias():
    d = deployment(BRANCH)
    d["meta"].pop("branchAlias")
    assert select_preview([d], BRANCH).url == "dpl.vercel.app"


def test_building_deployment_is_returned_but_not_ready():
    got = select_preview([deployment(BRANCH, state="BUILDING")], BRANCH)
    assert got is not None and got.ready is False


def test_empty_list():
    assert select_preview([], BRANCH) is None


# --- the protection check ---------------------------------------------------

class FakeResponse:
    def __init__(self, status, headers=None, reason="Unauthorized"):
        self.status_code, self.headers, self.reason = status, headers or {}, reason


def test_sso_redirect_counts_as_protected(monkeypatch):
    monkeypatch.setattr("ralph.vercel.requests.get", lambda *a, **k: FakeResponse(
        302, {"location": "https://vercel.com/sso-api?url=x&nonce=y"}))
    assert protection_of("https://x.vercel.app").protected


def test_sso_nonce_cookie_counts_as_protected(monkeypatch):
    monkeypatch.setattr("ralph.vercel.requests.get", lambda *a, **k: FakeResponse(
        302, {"set-cookie": "_vercel_sso_nonce=abc; Path=/"}))
    assert protection_of("https://x.vercel.app").protected


def test_401_counts_as_protected(monkeypatch):
    monkeypatch.setattr("ralph.vercel.requests.get",
                        lambda *a, **k: FakeResponse(401))
    assert protection_of("https://x.vercel.app").protected


def test_plain_200_is_not_protected(monkeypatch):
    monkeypatch.setattr("ralph.vercel.requests.get",
                        lambda *a, **k: FakeResponse(200))
    guard = protection_of("https://x.vercel.app")
    assert guard.protected is False and "reachable anonymously" in guard.detail


def test_app_level_redirect_is_not_protection(monkeypatch):
    """A 302 to the app's own /sign-in is NOT Vercel auth -- the page is public.
    This is the case that makes a naive status-code check wrong."""
    monkeypatch.setattr("ralph.vercel.requests.get", lambda *a, **k: FakeResponse(
        302, {"location": "https://x.vercel.app/sign-in"}))
    assert protection_of("https://x.vercel.app").protected is False


def test_unreachable_is_not_treated_as_protected(monkeypatch):
    """Failing closed here would report an unverified URL as safe."""
    import requests as r
    def boom(*a, **k):
        raise r.RequestException("dns failure")
    monkeypatch.setattr("ralph.vercel.requests.get", boom)
    assert protection_of("https://x.vercel.app").protected is False


def test_empty_url():
    assert protection_of("").protected is False
