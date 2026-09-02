#!/usr/bin/env python
"""Phase 0 acceptance check + a standing pre-run guard.

Run standalone to verify the machine is in a state where an unattended run is
safe. dispatch.sh calls this before every tick. Exit 0 = safe, 1 = do not run.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
from datetime import datetime

from ralph.config import (Config, ConfigError, STOP_FILE, assert_no_api_key,
                          configure_stdio, load)

OK, WARN, FAIL = "  ok  ", " warn ", " FAIL "

# Env vars needed per phase, so preflight stays useful before every phase is built.
ENV_BY_PHASE = {
    "1 (gate)": ["LINEAR_API_KEY"],
    "2 (agent)": ["GITHUB_TOKEN", "REPO_DIR"],
    "3 (preview)": ["VERCEL_TOKEN", "VERCEL_PROJECT_ID"],
    "4-5 (slack)": ["SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "SLACK_CHANNEL"],
    "6 (local tier)": ["OLLAMA_HOST"],
}


class Checks:
    def __init__(self) -> None:
        self.failed = 0
        self.warned = 0

    def record(self, level: str, name: str, detail: str = "") -> None:
        if level == FAIL:
            self.failed += 1
        elif level == WARN:
            self.warned += 1
        print(f"[{level}] {name}" + (f" - {detail}" if detail else ""))


def check_claude_auth() -> tuple[str, str]:
    """Confirm the CLI is authenticated via the subscription, not an API key."""
    exe = shutil.which("claude")
    if not exe:
        return FAIL, "`claude` not on PATH"
    try:
        proc = subprocess.run(
            [exe, "auth", "status"], capture_output=True, text=True, timeout=60
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return FAIL, f"`claude auth status` failed: {exc}"
    if proc.returncode != 0:
        return FAIL, "returned non-zero; run `claude login`"
    out = proc.stdout
    if '"loggedIn": true' not in out:
        return FAIL, "not logged in; run `claude login`"
    if '"apiProvider": "firstParty"' not in out:
        return FAIL, "apiProvider is not firstParty - billing may not be the subscription"
    # Independent second guard on the billing risk: verified empirically that
    # setting ANTHROPIC_API_KEY flips subscriptionType from "pro" to null, i.e.
    # the CLI stops treating the session as subscription-backed. A null tier
    # therefore means this run would NOT bill against the Pro plan.
    match = re.search(r'"subscriptionType":\s*(?:"([^"]+)"|null)', out)
    if not match or not match.group(1):
        return FAIL, (
            "subscriptionType is null - the run would not bill against the Pro "
            "subscription (usually means an API key is in the environment)"
        )
    return OK, f"claude.ai OAuth, {match.group(1)}"


def check_env(checks: Checks) -> None:
    """Missing env vars for later phases are warnings, not failures.

    Phase 0 must pass on a machine where phases 1-6 are not yet configured.
    """
    for phase, names in ENV_BY_PHASE.items():
        missing = [n for n in names if not os.environ.get(n)]
        if missing:
            checks.record(WARN, f"env for phase {phase}", f"unset: {', '.join(missing)}")
        else:
            checks.record(OK, f"env for phase {phase}")


def main() -> int:
    configure_stdio()
    checks = Checks()
    print("ralph preflight - " + datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("-" * 72)

    # 1. Billing guardrail - the single most important check (SS2, SS4).
    try:
        assert_no_api_key()
        checks.record(OK, "ANTHROPIC_API_KEY unset", "billing stays on the subscription")
    except ConfigError as exc:
        checks.record(FAIL, "ANTHROPIC_API_KEY", str(exc))

    # 2. Config parses and satisfies the hard constraints.
    cfg: Config | None = None
    try:
        cfg = load(check_env=False)
        checks.record(
            OK,
            "config.yaml",
            f"repo={cfg.repo['name']} model={cfg.loop['model']} "
            f"cap={cfg.max_runs_per_day}/day "
            f"windows={','.join(str(w) for w in cfg.windows)}",
        )
        checks.record(OK, "scout disabled", "self-found work is off for MVP")
    except ConfigError as exc:
        checks.record(FAIL, "config.yaml", str(exc))

    # 3. Subscription auth.
    level, detail = check_claude_auth()
    checks.record(level, "claude auth", detail)

    # 4. Operational state.
    if STOP_FILE.exists():
        checks.record(WARN, "STOP file present", f"{STOP_FILE} - ticks will no-op")
    else:
        checks.record(OK, "no STOP file")

    if cfg is not None:
        used, cap = cfg.runs_today(), cfg.max_runs_per_day
        level = WARN if used >= cap else OK
        checks.record(level, "daily run budget", f"{used}/{cap} used today")

        now = datetime.now()
        inside = cfg.in_window(now)
        checks.record(OK, "schedule window", f"now={now:%H:%M} inside={inside}")

    check_env(checks)

    print("-" * 72)
    verdict = "FAILED" if checks.failed else "PASSED"
    print(f"{verdict}: {checks.failed} failure(s), {checks.warned} warning(s)")
    return 1 if checks.failed else 0


if __name__ == "__main__":
    sys.exit(main())
