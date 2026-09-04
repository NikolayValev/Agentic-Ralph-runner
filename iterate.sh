#!/usr/bin/env bash
# Phase 2: run one guarded Claude iteration on the subscription.
# Reads a Task JSON object on stdin, writes an Agent Report JSON on stdout.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${RALPH_PYTHON:-python}"
LOG_DIR="${RALPH_LOG_DIR:-$HERE/logs}"
mkdir -p "$LOG_DIR"
RUN_ID="$(date +%Y%m%d-%H%M%S)"
RUN_LOG="$LOG_DIR/run-$RUN_ID.log"

to_win()   { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
to_posix() { if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s' "$1"; fi; }
log() { printf '%s %s\n' "$(date +'%Y-%m-%dT%H:%M:%S')" "$*" | tee -a "$RUN_LOG" >&2; }

CONFIG_ARGS=()
[[ -n "${RALPH_CONFIG:-}" ]] && CONFIG_ARGS=(--config "$(to_win "$RALPH_CONFIG")")

# The subscription guardrail, again: iterate.sh may be run by hand, not only via
# dispatch.sh, so it must not rely on dispatch having checked.
if [[ -n "${ANTHROPIC_API_KEY:-}" ]]; then
  log "FATAL: ANTHROPIC_API_KEY is set; refusing to run"
  exit 1
fi

if [[ -z "${REPO_DIR:-}" ]]; then
  log "FATAL: REPO_DIR is not set"
  exit 1
fi

TASK_JSON="$(cat)"
TICKET="$("$PYTHON" -c "import json,sys; print(json.loads(sys.argv[1])['ref'])" "$TASK_JSON")"
log "iterate: ticket=$TICKET run=$RUN_ID"

# --- config values ----------------------------------------------------------
read -r MODEL TIMEOUT_S PERM_MODE BASE BRANCH TEST_CMD LINT_CMD < <(
  "$PYTHON" - "$TICKET" <<'PY'
import sys
from ralph.config import load
cfg = load()
t = sys.argv[1]
print(cfg.loop["model"], cfg.loop["timeout_seconds"], cfg.raw["safety"]["permission_mode"],
      cfg.repo["default_branch"], cfg.branch_for(t),
      cfg.commands["test"].replace(" ", "\x1f"), cfg.commands["lint"].replace(" ", "\x1f"))
PY
)
TEST_CMD="${TEST_CMD//$'\x1f'/ }"; LINT_CMD="${LINT_CMD//$'\x1f'/ }"

# --- prepare the isolated checkout -----------------------------------------
if ! "$PYTHON" workspace.py --ticket "$TICKET" "${CONFIG_ARGS[@]}" >>"$RUN_LOG" 2>&1; then
  log "iterate: workspace preparation FAILED; see $RUN_LOG"
  printf '{"status":"error","mode":"tagged","ticket":"%s","branch":"%s","pr_url":"","preview_url":"","summary":"workspace preparation failed"}\n' "$TICKET" "$BRANCH"
  exit 1
fi

# --- build the prompt -------------------------------------------------------
PROMPT_FILE="$LOG_DIR/prompt-$RUN_ID.md"
"$PYTHON" - "$TICKET" "$BRANCH" "$BASE" "$TEST_CMD" "$LINT_CMD" "$PROMPT_FILE" <<'PY'
import os, sys, pathlib
from ralph.linear import _post

ticket, branch, base, test_cmd, lint_cmd, out = sys.argv[1:7]
query = """query I($id: String!) { issue(id: $id) { identifier title description } }"""
issue = _post(os.environ.get("LINEAR_API_KEY", ""), query, {"id": ticket})["issue"]

template = pathlib.Path("prompts/agent.md").read_text(encoding="utf-8")
filled = (template
    .replace("{TICKET}", issue["identifier"])
    .replace("{TITLE}", issue["title"] or "")
    .replace("{DESCRIPTION}", (issue.get("description") or "").strip())
    .replace("{REPO_DIR}", os.environ["REPO_DIR"])
    .replace("{BRANCH}", branch).replace("{BASE}", base)
    .replace("{TEST_CMD}", test_cmd).replace("{LINT_CMD}", lint_cmd))
pathlib.Path(out).write_text(filled, encoding="utf-8")
print(f"prompt written: {len(filled)} chars")
PY

# --- move the ticket to In Progress (deterministic, not the agent's job) ----
# Skipped under RALPH_DRY_RUN: finalize returns before it would transition the
# ticket onward, so moving it here would strand it In Progress with no PR --
# a board that says someone is working a ticket nobody is working.
if [[ -z "${RALPH_DRY_RUN:-}" ]]; then
"$PYTHON" - "$TICKET" >>"$RUN_LOG" 2>&1 <<'PY' || true
import os, sys
from ralph.config import load
from ralph.linear import move_issue
cfg = load()
print("moved to", move_issue(os.environ["LINEAR_API_KEY"], sys.argv[1],
                             cfg.linear["in_progress_state"], cfg.linear["team_key"]))
PY
else
  log "dry run: leaving $TICKET in its current state"
fi

# --- the guarded Claude run -------------------------------------------------
# `timeout` is the ONLY enforced bound on the iteration: Claude Code 2.1.248 has
# no --max-turns flag, so loop.max_turns cannot be applied here.
mapfile -t ALLOWED < <("$PYTHON" -c "
from ralph.config import load
[print(t) for t in load().raw['safety']['allowed_tools']]")

AGENT_OUT="$LOG_DIR/agent-$RUN_ID.txt"
log "iterate: running claude (model=$MODEL, mode=$PERM_MODE, timeout=${TIMEOUT_S}s, ${#ALLOWED[@]} tools)"

# Claude must run WITH THE CHECKOUT AS ITS WORKING DIRECTORY. Its sandbox is
# scoped to the cwd: invoked from the ralph project it could read ralph's own
# source but not the repo it is meant to change, and returned an error having
# touched nothing. Run it in a subshell so this script keeps its relative paths.
REPO_DIR_POSIX="$(to_posix "$REPO_DIR")"
PROMPT_ABS="$(to_posix "$PROMPT_FILE")"

set +e
(
  cd "$REPO_DIR_POSIX" || exit 1
  timeout --kill-after=60 "$TIMEOUT_S" \
    claude -p "$(cat "$PROMPT_ABS")" \
      --model "$MODEL" \
      --output-format json \
      --permission-mode "$PERM_MODE" \
      --allowedTools "${ALLOWED[@]}" \
      --strict-mcp-config
) > "$AGENT_OUT" 2>>"$RUN_LOG"
CLAUDE_RC=$?
set -e

if [[ $CLAUDE_RC -eq 124 || $CLAUDE_RC -eq 137 ]]; then
  log "iterate: TIMED OUT after ${TIMEOUT_S}s (this is the one-increment bound)"
elif [[ $CLAUDE_RC -ne 0 ]]; then
  log "iterate: claude exited rc=$CLAUDE_RC"
fi

# Unwrap --output-format json so finalize sees the agent's own text.
"$PYTHON" - "$AGENT_OUT" <<'PY'
import json, pathlib, sys
p = pathlib.Path(sys.argv[1])
raw = p.read_text(encoding="utf-8", errors="replace")
for line in raw.splitlines():
    line = line.strip()
    if line.startswith("{"):
        try:
            d = json.loads(line)
        except Exception:
            continue
        if "result" in d:
            p.write_text(str(d["result"]), encoding="utf-8")
            break
PY

# --- verify, push, PR, transition ------------------------------------------
# finalize prints the Agent Report JSON as its last stdout line; notify.py reads
# that line. Captured to a file rather than piped straight through so FINAL_RC is
# finalize's own exit code and not the pipeline's.
REPORT_FILE="$LOG_DIR/report-$RUN_ID.json"
# errexit OFF across this pipeline, deliberately. finalize exits non-zero on
# outcomes that still print a report and still deserve a Slack message -- a
# forbidden-path rejection, and "the branch pushed but the PR could not be
# opened". Under `set -e` the script would die on this line: FINAL_RC would
# never be read, notify would never run, and the failures most worth hearing
# about would be the silent ones.
set +e
"$PYTHON" finalize.py --ticket "$TICKET" --output "$(to_win "$AGENT_OUT")" "${CONFIG_ARGS[@]}" ${RALPH_DRY_RUN:+--dry-run} | tee "$REPORT_FILE"
FINAL_RC=${PIPESTATUS[0]}
set -e

# --- tell the human ---------------------------------------------------------
# Never fatal: a Slack outage must not turn a completed run into a failed tick,
# and the PR exists either way. A silent-status report posts nothing by design.
if [[ -n "${RALPH_DRY_RUN:-}" ]]; then
  # A dry run never pushes, so its report carries an empty pr_url, and the
  # contract rightly refuses to tell a human "ready for review" with no link
  # to review. Calling notify here could only ever fail; skip it and say so
  # rather than logging a failure that means nothing is wrong.
  log "iterate: dry run; not notifying (no PR to link to)"
elif [[ -s "$REPORT_FILE" ]]; then
  if ! "$PYTHON" notify.py --report-file "$(to_win "$REPORT_FILE")" "${CONFIG_ARGS[@]}" >>"$RUN_LOG" 2>&1; then
    log "iterate: notify failed (tick unaffected; see $RUN_LOG)"
  fi
else
  log "iterate: no report produced; nothing to notify"
fi

log "iterate: done (finalize rc=$FINAL_RC, log $RUN_LOG)"
exit $FINAL_RC
