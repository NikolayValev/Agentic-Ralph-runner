"""Config + environment loading, with the hard guardrails from plan v2 sec.4.

Two things this module refuses to let through:
  1. ANTHROPIC_API_KEY being set  -- would silently move billing off the Pro
     subscription onto metered API tokens (sec.2).
  2. scout.enabled being true     -- self-found work is out of scope for MVP (sec.3).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, time
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config.yaml"
# Relocatable so tests stay hermetic and so the loop can run with the checkout
# read-only (SS4: unprivileged user, isolated checkout).
STATE_DIR = Path(os.environ.get("RALPH_STATE_DIR") or (ROOT / "state"))
STOP_FILE = STATE_DIR / "STOP"

_WINDOW_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")

REQUIRED_SECTIONS = (
    "repo", "linear", "schedule", "loop", "local", "scout",
    "deploy", "notify", "commands", "safety", "conversation",
)

# Modes that make --allowedTools decorative. Verified empirically: under
# acceptEdits a bash write outside the allowlist succeeded; under manual the
# same write was blocked. Allowing these would void the "scoped tools only"
# constraint while still looking correctly configured.
UNSAFE_PERMISSION_MODES = ("acceptEdits", "bypassPermissions", "dontAsk")


def configure_stdio() -> None:
    """Make stdout/stderr tolerate arbitrary text on a Windows console.

    Ticket titles come from Linear and contain characters cp1252 cannot encode.
    Unattended runs redirect to a log file, where the default encoding would
    raise UnicodeEncodeError and kill the tick over a dash in a title.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


class ConfigError(RuntimeError):
    """Configuration is missing, malformed, or violates a hard constraint."""


def assert_no_api_key(env: dict | None = None) -> None:
    """Fail loudly if ANTHROPIC_API_KEY is set.

    In `claude -p` mode a present API key takes precedence over the claude.ai
    OAuth login, so an accidentally-exported key turns every scheduled run into
    a metered API charge. This is a hard stop, not a warning.
    """
    env = os.environ if env is None else env
    if env.get("ANTHROPIC_API_KEY"):
        raise ConfigError(
            "ANTHROPIC_API_KEY is set. Ralph runs on the Claude Pro subscription "
            "via `claude login`; a present API key would be used in preference to "
            "it and bill per-token. Unset it before running."
        )


@dataclass(frozen=True)
class Window:
    start: time
    end: time

    def contains(self, moment: time) -> bool:
        if self.start <= self.end:
            return self.start <= moment < self.end
        return moment >= self.start or moment < self.end  # wraps midnight

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}"


def parse_window(raw: str) -> Window:
    match = _WINDOW_RE.match(raw.strip())
    if not match:
        raise ConfigError(f"bad schedule window {raw!r}; expected HH:MM-HH:MM")
    sh, sm, eh, em = (int(g) for g in match.groups())
    if (sh, sm) == (eh, em):
        raise ConfigError(f"schedule window {raw!r} is zero-length")
    return Window(time(sh, sm), time(eh, em))


@dataclass(frozen=True)
class Config:
    raw: dict
    path: Path

    # -- convenience accessors, so callers don't stringly-index the dict --
    @property
    def repo(self) -> dict: return self.raw["repo"]
    @property
    def linear(self) -> dict: return self.raw["linear"]
    @property
    def loop(self) -> dict: return self.raw["loop"]
    @property
    def local(self) -> dict: return self.raw["local"]

    @property
    def conversation(self) -> dict: return self.raw["conversation"]
    @property
    def deploy(self) -> dict: return self.raw["deploy"]
    @property
    def commands(self) -> dict: return self.raw["commands"]

    @property
    def windows(self) -> list[Window]:
        return [parse_window(w) for w in self.raw["schedule"]["windows"]]

    @property
    def max_runs_per_day(self) -> int:
        return int(self.raw["schedule"]["max_runs_per_day"])

    def branch_for(self, ticket: str) -> str:
        return f"{self.repo['branch_prefix']}{ticket}"

    # -- schedule / cap / stop guards --------------------------------------
    def in_window(self, moment: datetime) -> bool:
        return any(w.contains(moment.time()) for w in self.windows)

    def runs_today_path(self, day: date | None = None) -> Path:
        day = day or date.today()
        return STATE_DIR / f"runs-{day.isoformat()}"

    def runs_today(self, day: date | None = None) -> int:
        path = self.runs_today_path(day)
        if not path.exists():
            return 0
        try:
            return int(path.read_text(encoding="utf-8").strip() or 0)
        except ValueError:
            raise ConfigError(f"run counter {path} is corrupt; delete it to reset")

    def increment_runs_today(self, day: date | None = None) -> int:
        path = self.runs_today_path(day)
        path.parent.mkdir(parents=True, exist_ok=True)
        count = self.runs_today(day) + 1
        path.write_text(str(count), encoding="utf-8")
        return count

    def cap_reached(self, day: date | None = None) -> bool:
        return self.runs_today(day) >= self.max_runs_per_day

    @staticmethod
    def stopped() -> bool:
        return STOP_FILE.exists()


def forbidden_hits(changed_paths: list[str], forbidden: list[str]) -> list[str]:
    """Which changed paths fall under a forbidden prefix.

    Guards against the agent editing files that would widen its own permissions
    (this repo commits .claude/settings.local.json granting "Bash(pnpm exec *)").
    """
    hits = []
    for changed in changed_paths:
        normalised = changed.replace("\\", "/").lstrip("./")
        for prefix in forbidden:
            prefix_n = prefix.replace("\\", "/").lstrip("./")
            if normalised == prefix_n.rstrip("/") or normalised.startswith(prefix_n.rstrip("/") + "/"):
                hits.append(changed)
                break
    return hits


def _validate(raw: dict, path: Path) -> None:
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} did not parse to a mapping")
    for section in REQUIRED_SECTIONS:
        if section not in raw:
            raise ConfigError(f"{path} is missing required section {section!r}")

    if raw["scout"].get("enabled"):
        raise ConfigError(
            "scout.enabled is true. Self-found work is out of scope for the MVP "
            "(plan v2 sec.3); enabling it requires a plan revision, not a config flip."
        )

    model = raw["loop"].get("model")
    if model != "sonnet":
        raise ConfigError(
            f"loop.model is {model!r}; must be 'sonnet'. Opus on Pro requires "
            "metered credits, which are deliberately disabled (sec.2)."
        )

    if int(raw["schedule"]["max_runs_per_day"]) < 1:
        raise ConfigError("schedule.max_runs_per_day must be >= 1")
    if not raw["schedule"]["windows"]:
        raise ConfigError("schedule.windows is empty; the loop would never run")
    for window in raw["schedule"]["windows"]:
        parse_window(window)

    if int(raw["loop"].get("timeout_seconds", 0)) < 60:
        raise ConfigError(
            "loop.timeout_seconds must be >= 60. It is the only ENFORCED bound on a "
            "run: Claude Code 2.1.248 has no --max-turns flag."
        )

    mode = raw["safety"].get("permission_mode")
    if mode in UNSAFE_PERMISSION_MODES:
        raise ConfigError(
            f"safety.permission_mode is {mode!r}, which makes --allowedTools "
            "decorative: tools outside the allowlist still run. Use 'manual'."
        )
    if not raw["safety"].get("allowed_tools"):
        raise ConfigError("safety.allowed_tools is empty; the agent would have no tools")

    test_cmd = raw["commands"].get("test", "")
    if re.search(r"\bvitest\b", test_cmd) and not re.search(r"\brun\b", test_cmd):
        raise ConfigError(
            f"commands.test is {test_cmd!r}: bare vitest starts WATCH mode and will "
            "hang a headless run. Use 'vitest run'."
        )


def load(path: Path | str = CONFIG_PATH, *, check_env: bool = True) -> Config:
    path = Path(path)
    if check_env:
        assert_no_api_key()
    if not path.exists():
        raise ConfigError(f"config file not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    _validate(raw, path)
    return Config(raw=raw, path=path)
