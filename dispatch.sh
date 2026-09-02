#!/usr/bin/env bash
# Ralph cron entry (plan v2 §6). One tick: lock, guard, gate, orchestrate.
#
# Host is native Windows; this runs under Git Bash, driven by Task Scheduler.
# Exit 0 always unless the tick itself broke -- a no-op tick (nothing eligible,
# outside window, capped, stopped) is a normal outcome, not a failure, and must
# not make Task Scheduler report a failed task.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${RALPH_PYTHON:-python}"

# This script runs under Git Bash but drives a NATIVE WINDOWS Python. The two
# disagree about paths: bash's /tmp/x is C:/tmp/x to Python, not the msys temp
# dir. So normalise at the boundary -- POSIX form for anything bash touches,
# Windows form for anything handed to Python. Without this, dispatch and gate
# can silently use different state dirs and STOP / the run cap stop working.
to_posix() { if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s' "$1"; fi; }
to_win()   { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

STATE_DIR="$(to_posix "${RALPH_STATE_DIR:-$HERE/state}")"
LOG_DIR="$(to_posix "${RALPH_LOG_DIR:-$HERE/logs}")"
LOCK_DIR="$STATE_DIR/dispatch.lock"
# The PID file is a SIBLING, never inside LOCK_DIR: the lock dir must stay empty
# so `rmdir` can release it. (Putting the pid inside made rmdir fail silently and
# wedged the schedule until the stale-lock timeout.)
LOCK_PID_FILE="$STATE_DIR/dispatch.lock.pid"
LOCK_MAX_AGE_SECONDS="${RALPH_LOCK_MAX_AGE:-7200}"   # 2h; longer than loop.timeout_seconds
# Alternate config, for testing and for running a second repo off one checkout.
CONFIG_ARGS=()
[[ -n "${RALPH_CONFIG:-}" ]] && CONFIG_ARGS=(--config "$(to_win "$(to_posix "$RALPH_CONFIG")")")

mkdir -p "$STATE_DIR" "$LOG_DIR"
# Re-export in Windows form so the Python children resolve the same directory.
export RALPH_STATE_DIR="$(to_win "$STATE_DIR")"
LOG_FILE="$LOG_DIR/dispatch-$(date +%Y-%m-%d).log"

log() { printf '%s %s\n' "$(date +'%Y-%m-%dT%H:%M:%S')" "$*" | tee -a "$LOG_FILE" >&2; }

# --- .env -------------------------------------------------------------------
# Loaded with `set -a` so exports reach the child processes. Never echoed.
if [[ -f "$HERE/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$HERE/.env"
  set +a
fi

# The one billing guardrail that must hold before anything else runs (§2, §4).
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  log "FATAL: ANTHROPIC_API_KEY is set; refusing to run (would bill per-token)"
  exit 1
fi

# --- lock -------------------------------------------------------------------
# mkdir is atomic on NTFS, so it works as a lock without flock (absent in Git Bash).
release_lock() { rm -f "$LOCK_PID_FILE" 2>/dev/null || true; rmdir "$LOCK_DIR" 2>/dev/null || true; }

if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  lock_age=$(( $(date +%s) - $(stat -c %Y "$LOCK_DIR" 2>/dev/null || date +%s) ))
  if (( lock_age > LOCK_MAX_AGE_SECONDS )); then
    log "WARN: breaking stale lock (age ${lock_age}s > ${LOCK_MAX_AGE_SECONDS}s)"
    release_lock
    mkdir "$LOCK_DIR" 2>/dev/null || { log "could not take lock after breaking it"; exit 0; }
  else
    log "another tick holds the lock (age ${lock_age}s); skipping"
    exit 0
  fi
fi
trap release_lock EXIT
echo "$$" > "$LOCK_PID_FILE" 2>/dev/null || true

# --- log retention ----------------------------------------------------------
"$PYTHON" runs.py prune "${CONFIG_ARGS[@]}" >>"$LOG_FILE" 2>&1 || true

# --- preflight --------------------------------------------------------------
if ! "$PYTHON" preflight.py >>"$LOG_FILE" 2>&1; then
  log "preflight FAILED; see $LOG_FILE"
  exit 1
fi

# --- gate -------------------------------------------------------------------
set +e
TASK_JSON="$("$PYTHON" gate.py --explain "${CONFIG_ARGS[@]}" 2>>"$LOG_FILE")"
GATE_RC=$?
set -e

case "$GATE_RC" in
  0)  log "gate: work found: $TASK_JSON" ;;
  10) log "gate: nothing eligible; tick is a no-op";      exit 0 ;;
  11) log "gate: outside schedule window; tick is a no-op"; exit 0 ;;
  12) log "gate: per-day run cap reached; tick is a no-op"; exit 0 ;;
  13) log "gate: STOP file present; tick is a no-op";      exit 0 ;;
  *)  log "gate: ERROR (rc=$GATE_RC); see $LOG_FILE";      exit 1 ;;
esac

# --- Tier 1: local pre-filter (free) -----------------------------------------
# The point of this step is that a Claude turn is never spent on a tick the
# local model can settle. It fails OPEN: if Ollama is down or unsure, the
# ticket still reaches Claude. Saving a turn must never cost a ticket.
set +e
PREFILTER="$(echo "$TASK_JSON" | "$PYTHON" local_triage.py "${CONFIG_ARGS[@]}" 2>>"$LOG_FILE")"
TRIAGE_RC=$?
set -e
if [[ $TRIAGE_RC -ne 0 || -z "$PREFILTER" ]]; then
  log "triage: failed (rc=$TRIAGE_RC); routing to Claude"
  PREFILTER='{"run":true,"tier":"claude","reason":"triage failed","commit_hint":""}'
fi

read -r PF_RUN PF_TIER < <("$PYTHON" -c "
import json, sys
pf = json.loads(sys.argv[1])
print(str(pf['run']).lower(), pf['tier'])" "$PREFILTER")
log "triage: run=$PF_RUN tier=$PF_TIER"

TICKET="$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['ref'])" "$TASK_JSON")"

if [[ "$PF_RUN" != "true" ]]; then
  # Resolved locally, zero Claude usage. De-queue so it does not stall in the
  # window forever; the comment says a local model made the call, and re-adding
  # the label re-queues it.
  REASON="$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['reason'])" "$PREFILTER")"
  log "triage: no Claude turn spent; de-queueing $TICKET"
  "$PYTHON" - "$TICKET" "$REASON" >>"$LOG_FILE" 2>&1 <<'PY' || true
import sys
from ralph.config import load
import finalize
finalize.dequeue(load(check_env=False), sys.argv[1], "skipped by local triage", sys.argv[2])
PY
  exit 0
fi

# --- Tier 0: mechanical, no model at all -------------------------------------
if [[ "$PF_TIER" == "tooling" ]]; then
  set +e
  ./autofix.sh "$TICKET" >>"$LOG_FILE" 2>&1
  FIX_RC=$?
  set -e
  case "$FIX_RC" in
    0)  log "tier 0: committed an autofix; no Claude turn spent"; exit 0 ;;
    20) log "tier 0: nothing to fix; escalating to Claude" ;;
    *)  log "tier 0: failed (rc=$FIX_RC); escalating to Claude" ;;
  esac
fi

# --- Tier 2: the Claude iteration --------------------------------------------
# Order matters: bump the counter BEFORE invoking the agent, so a run that
# crashes still consumes its slot and cannot be retried in a tight loop.
if [[ -x "$HERE/iterate.sh" ]]; then
  "$PYTHON" runs.py bump "${CONFIG_ARGS[@]}" >>"$LOG_FILE" 2>&1 || { log "run cap hit at bump; aborting"; exit 0; }
  echo "$TASK_JSON" | "$HERE/iterate.sh"
else
  log "iterate.sh not present; stopping after the gate"
fi

log "tick complete"
