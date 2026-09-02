#!/usr/bin/env bash
# Tier 0 (plan sec.5): mechanical fixes with NO model and no Claude turn.
#
# Runs the repo's own lint autofixer in the isolated checkout and commits the
# result if anything changed. Costs nothing, so it is safe on every tick.
#
# Exit codes:  0 changes committed | 20 nothing to fix | 1 error

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"

PYTHON="${RALPH_PYTHON:-python}"
to_posix() { if command -v cygpath >/dev/null 2>&1; then cygpath -u "$1"; else printf '%s' "$1"; fi; }

TICKET="${1:-}"
[[ -z "${REPO_DIR:-}" ]] && { echo "autofix: REPO_DIR is not set" >&2; exit 1; }
REPO="$(to_posix "$REPO_DIR")"
[[ -d "$REPO/.git" ]] || { echo "autofix: $REPO is not a checkout" >&2; exit 1; }

# One command per line: simpler and safer than packing them into one line.
mapfile -t CMDS < <("$PYTHON" - <<'PY'
from ralph.config import load
cfg = load(check_env=False)
# eslint needs --fix to change anything; without it this is a no-op check.
print(cfg.commands["lint"] + " --fix")
print(cfg.commands["test"])
print(cfg.commands["install"])
PY
)
LINT_FIX="${CMDS[0]}"; TEST_CMD="${CMDS[1]}"; INSTALL_CMD="${CMDS[2]}"

cd "$REPO"

# Dependencies must exist, or every command below fails in a way that looks
# exactly like "nothing to fix". That false clean report happened once already.
if [[ ! -d node_modules ]]; then
  echo "autofix: installing dependencies" >&2
  if ! eval "$INSTALL_CMD" >/dev/null 2>&1; then
    echo "autofix: dependency install FAILED; cannot run tier 0" >&2
    exit 1
  fi
fi

echo "autofix: running '$LINT_FIX'" >&2
LINT_OUT="$(mktemp)"
set +e
eval "$LINT_FIX" >"$LINT_OUT" 2>&1
set -e

# A missing toolchain is an ERROR, not a clean result. eslint also exits
# non-zero for unfixable lint problems, so the exit code alone cannot tell
# those two apart -- hence matching on the message.
if grep -qiE "not recognized|command not found|ERR_PNPM|ENOENT" "$LINT_OUT"; then
  echo "autofix: lint could not run:" >&2
  tail -3 "$LINT_OUT" >&2
  rm -f "$LINT_OUT"
  exit 1
fi
rm -f "$LINT_OUT"

if [[ -z "$(git status --porcelain)" ]]; then
  echo "autofix: nothing to fix" >&2
  exit 20
fi

CHANGED="$(git status --porcelain | wc -l | tr -d ' ')"
echo "autofix: $CHANGED file(s) changed; verifying before committing" >&2

# Never commit an autofix that breaks the suite. A "free" tier that lands a red
# branch is not free.
set +e
eval "$TEST_CMD" >/dev/null 2>&1
TEST_RC=$?
set -e
if [[ $TEST_RC -ne 0 ]]; then
  echo "autofix: tests FAILED after autofix; reverting" >&2
  git checkout -- .
  exit 1
fi

git add -A
git commit -q -m "style: apply lint autofix${TICKET:+ ($TICKET)}" \
  -m "Tier-0 mechanical fix applied by ralph. No model was used."
echo "autofix: committed $CHANGED file(s)" >&2
exit 0
