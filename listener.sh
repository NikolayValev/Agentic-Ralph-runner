#!/usr/bin/env bash
# Long-running Slack listener (plan v2 phase 5), started by Task Scheduler at
# logon. Separate from dispatch.sh on purpose: the tick is a short batch job,
# this is a daemon, and a crash in one must not take out the other.
#
# Task Scheduler restarts this on failure. Everything it prints goes to a dated
# log so a silent death is diagnosable after the fact.

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${RALPH_PYTHON:-python}"

to_posix() { if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s' "$1"; fi; }
to_win()   { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

STATE_DIR="$(to_posix "${RALPH_STATE_DIR:-$HERE/state}")"
LOG_DIR="$(to_posix "${RALPH_LOG_DIR:-$HERE/logs}")"
mkdir -p "$STATE_DIR" "$LOG_DIR"
export RALPH_STATE_DIR="$(to_win "$STATE_DIR")"

LOG_FILE="$LOG_DIR/listener-$(date +%Y-%m-%d).log"
log() { printf '%s %s\n' "$(date +'%Y-%m-%dT%H:%M:%S')" "$*" | tee -a "$LOG_FILE" >&2; }

if [[ -f "$HERE/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERE/.env"
  set +a
fi

# Same billing guardrail as the tick: this process shells out to nothing today,
# but it can trigger work, and the check is cheap.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  log "FATAL: ANTHROPIC_API_KEY is set; refusing to start"
  exit 1
fi

if [[ -z "${SLACK_BOT_TOKEN:-}" || -z "${SLACK_APP_TOKEN:-}" ]]; then
  log "FATAL: SLACK_BOT_TOKEN and SLACK_APP_TOKEN must both be set"
  exit 1
fi

# Single instance: a second listener would double-handle every slash command.
PID_FILE="$STATE_DIR/listener.pid"
if [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
  log "listener already running (pid $(cat "$PID_FILE")); exiting"
  exit 0
fi
echo $$ > "$PID_FILE"
trap 'rm -f "$PID_FILE"' EXIT

log "listener starting"
"$PYTHON" slack_listener.py >>"$LOG_FILE" 2>&1
rc=$?
log "listener exited rc=$rc"
exit $rc
