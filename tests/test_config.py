"""Phase 0 AC: config loads, and the hard constraints fail loudly."""

from __future__ import annotations

from datetime import date, datetime, time

import pytest
import yaml

from ralph.config import Config, ConfigError, assert_no_api_key, load, parse_window


def test_real_config_loads():
    cfg = load(check_env=False)
    assert cfg.loop["model"] == "sonnet"
    assert cfg.raw["scout"]["enabled"] is False
    assert cfg.max_runs_per_day >= 1
    assert cfg.windows


# --- the billing guardrail (SS2, SS4) --------------------------------------

def test_api_key_set_fails_loudly():
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is set"):
        assert_no_api_key({"ANTHROPIC_API_KEY": "sk-ant-whatever"})


def test_api_key_absent_passes():
    assert_no_api_key({})


def test_api_key_empty_string_passes():
    """An exported-but-empty var does not authenticate, so it must not block."""
    assert_no_api_key({"ANTHROPIC_API_KEY": ""})


# --- other hard constraints -------------------------------------------------

def _write(tmp_path, mutate):
    raw = yaml.safe_load(open("config.yaml", encoding="utf-8").read())
    mutate(raw)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(raw), encoding="utf-8")
    return path


def test_scout_enabled_is_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r["scout"].update(enabled=True))
    with pytest.raises(ConfigError, match="scout.enabled is true"):
        load(path, check_env=False)


def test_opus_is_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r["loop"].update(model="opus"))
    with pytest.raises(ConfigError, match="metered credits"):
        load(path, check_env=False)


def test_watch_mode_test_command_is_rejected(tmp_path):
    """Bare `vitest` watches forever and would hang an unattended run."""
    path = _write(tmp_path, lambda r: r["commands"].update(test="pnpm exec vitest"))
    with pytest.raises(ConfigError, match="WATCH mode"):
        load(path, check_env=False)


def test_short_timeout_is_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r["loop"].update(timeout_seconds=5))
    with pytest.raises(ConfigError, match="only ENFORCED bound"):
        load(path, check_env=False)


def test_empty_windows_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r["schedule"].update(windows=[]))
    with pytest.raises(ConfigError, match="would never run"):
        load(path, check_env=False)


def test_missing_section_rejected(tmp_path):
    path = _write(tmp_path, lambda r: r.pop("deploy"))
    with pytest.raises(ConfigError, match="missing required section 'deploy'"):
        load(path, check_env=False)


# --- schedule windows -------------------------------------------------------

@pytest.mark.parametrize("bad", ["1:00-6:00", "01:00", "25:00-26:00", "01:00-01:00", ""])
def test_bad_windows_rejected(bad):
    with pytest.raises(ConfigError):
        parse_window(bad)


def test_window_membership():
    w = parse_window("01:00-06:00")
    assert w.contains(time(1, 0))
    assert w.contains(time(5, 59))
    assert not w.contains(time(6, 0))       # end is exclusive
    assert not w.contains(time(0, 59))


def test_window_wrapping_midnight():
    w = parse_window("22:00-02:00")
    assert w.contains(time(23, 30))
    assert w.contains(time(0, 30))
    assert not w.contains(time(12, 0))


def test_in_window_uses_all_windows():
    cfg = load(check_env=False)
    assert cfg.in_window(datetime(2026, 9, 1, 3, 0))     # 01:00-06:00
    assert cfg.in_window(datetime(2026, 9, 1, 12, 30))   # 12:00-13:00
    assert not cfg.in_window(datetime(2026, 9, 1, 17, 0))


# --- per-day run cap --------------------------------------------------------

def test_run_cap_counts_and_trips(tmp_path, monkeypatch):
    import ralph.config as mod

    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    cfg = load(check_env=False)
    day = date(2026, 9, 1)

    assert cfg.runs_today(day) == 0
    assert not cfg.cap_reached(day)

    for expected in range(1, cfg.max_runs_per_day + 1):
        assert cfg.increment_runs_today(day) == expected

    assert cfg.cap_reached(day)


def test_corrupt_counter_fails_loudly(tmp_path, monkeypatch):
    import ralph.config as mod

    monkeypatch.setattr(mod, "STATE_DIR", tmp_path)
    cfg = load(check_env=False)
    day = date(2026, 9, 1)
    cfg.runs_today_path(day).write_text("not-a-number", encoding="utf-8")
    with pytest.raises(ConfigError, match="corrupt"):
        cfg.runs_today(day)


def test_branch_naming():
    cfg = load(check_env=False)
    assert cfg.branch_for("NIK-111") == "ralph/NIK-111"
