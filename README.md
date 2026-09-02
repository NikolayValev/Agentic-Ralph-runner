# ralph — autonomous ticket agent (MVP)

Implements *Implementation Plan v2 — Autonomous Ticket Agent (Ralph MVP)*.
Picks up Linear tickets labeled `autonomous-eligible`, does **one increment per
run**, opens a PR **left in review**, reports to Slack. Never merges, never
deploys to production.

## Status

| Phase | State |
|---|---|
| 0 — Skeleton & contracts | **done, verified** |
| 1 — Gate + schedule guardrails | **done, verified live** |
| 2 — Iteration on the subscription | **built; verified live except the push/PR path** (needs `GITHUB_TOKEN`) |
| 3 — Protected staging | **done, verified live** (API lookup + protection) |
| 4 — Slack outbound | **built; payload verified.** Send untested (needs `SLACK_BOT_TOKEN`) |
| 5 — Slack inbound | **built; commands verified live.** Socket Mode untested (needs `SLACK_APP_TOKEN`) |
| 6 — Local triage tier | **done, verified live** with `gpt-oss:20b` |
| 7 — Circuit breaker | **done, verified live** |

**Do not install the schedule until phases 1–5 pass by hand.**

## Verified on this machine (2026-09-01)

- `claude` 2.1.248, authenticated `claude.ai` / `firstParty` / `subscriptionType: pro`.
- `ANTHROPIC_API_KEY` unset. Setting it flips `subscriptionType` to `null` —
  empirical confirmation of plan §10.2. `preflight.py` guards on both the env var
  and the CLI's own reported tier.
- Flags present: `-p`, `--output-format json`, `--model sonnet`, `--allowedTools`,
  `--permission-mode`, `--mcp-config`, `--strict-mcp-config`, `--fallback-model`.
- Linear team key `NIK`; states `Todo` / `In Progress` / `In Review` all exist.
- Ollama 0.33.2 serving on :11434, **no models pulled**.

## Deviations from plan v2

1. **`--max-turns` does not exist** in Claude Code 2.1.248 (§7, §9 Phase 2, §10.4
   assume it). `loop.max_turns` is retained as an *advisory* number injected into
   the prompt; the **enforced** bound is `loop.timeout_seconds`, applied by
   `iterate.sh` to the process.
2. **`GITHUB_TOKEN` added** to the env contract. §7 omits any GitHub credential,
   but Phase 2 must push a branch and open a PR, and `gh` is not installed here.
3. **Host is native Windows**, not WSL2 (§3 "WSL2 assumed"). The whole toolchain
   — Claude Code, Ollama-on-GPU, git — is already installed and working natively;
   WSL2 would need a second install and a second `claude login`. Scripts run under
   Git Bash; the clock is Task Scheduler, not cron.
4. **`--allowedTools` syntax is unverified.** The CLI's help shows `Bash(git *)`
   (space); the plan writes `Bash(git:*)` (colon). A scoped-tools guardrail that
   mis-parses fails the §4 constraint silently, so Phase 2 must verify the parse
   empirically before relying on it.
5. **`commands.test` is `pnpm exec vitest run`,** not `pnpm test`. The repo's
   `test` script is bare `vitest`, which starts watch mode and would hang an
   unattended run. `config.py` rejects a watch-mode test command.
6. **Opting in takes two gestures, not one.** The plan says "oldest *unstarted*
   tagged ticket". In this workspace `Todo` is `unstarted` and `Backlog` is
   `backlog`, so a labelled ticket sitting in Backlog is treated as *parked*, and
   only moves into scope when you drag it to Todo. This is deliberate: the label
   alone cannot put a ticket in front of the agent.

## Outstanding prerequisites (human)

- [x] ~~Create the `autonomous-eligible` label in Linear~~ — created 2026-09-01.
- [x] ~~`LINEAR_API_KEY`~~ — added; live gate, state lookup and 401 handling all verified.
- [ ] `GITHUB_TOKEN` — the only thing blocking PR creation. `git push` needs nothing
      (it uses the machine's credential helper), but the REST call to open the PR does.
- [ ] `LINEAR_API_KEY`, `GITHUB_TOKEN`, `VERCEL_TOKEN` in `.env`.
- [ ] `ollama pull gpt-oss:20b` (Phase 6).
- [ ] Slack app: bot scopes `chat:write`,`commands`; Socket Mode token; `/ralph`.
- [ ] Vercel Deployment Protection enabled for Preview.
- [ ] `pip install -r requirements.txt` (`slack-sdk` is not yet installed).
- [ ] Disable PC sleep, or the schedule will not fire.

## Layout

```
dispatch.sh        the tick: lock, kill-switch, guards, three-tier routing
gate.py            picks the ticket, or exits with a reason (exit codes below)
local_triage.py    tier 1: is this worth a Claude turn? (local model, free)
autofix.sh         tier 0: lint autofix, no model at all
iterate.sh         tier 2: the guarded `claude -p` run
finalize.py        verify -> reject forbidden edits -> push -> PR -> preview -> transition
notify.py          posts the report to Slack
slack_listener.py  Socket Mode: buttons and /ralph
runs.py            run counter, streak, prune, stop/go
workspace.py       isolated checkout (refuses to touch a working copy)
install-schedule.ps1  Task Scheduler registration (does not self-run)
prompts/agent.md   the agent's program
ralph/             linear, github, vercel, ollama, slack, commands, breaker,
                   config, contracts
config.yaml        policy: windows, run cap, model, allowed tools, breaker
.env.example       secrets contract (ANTHROPIC_API_KEY deliberately absent)
preflight.py       Phase 0 AC + pre-run guard; dispatch calls it every tick
ralph/config.py    config loading + hard constraints (billing, scout, model, cap)
ralph/contracts.py the three §7 JSON contracts: Task, PreFilter, AgentReport
tests/             173 tests: guardrails, contracts, gate, finalize, prompt, scripts
state/             STOP file, per-day run counters (gitignored)
```

## Use

```bash
python preflight.py                       # exit 0 = safe to run unattended
python -m pytest tests/ -q                # 173 tests
./dispatch.sh                             # one tick (no-op outside the window)
python runs.py show|bump|stop|go          # counter + kill switch
python gate.py --ignore-schedule --explain --issues-file fixture.json
```

### Slack commands

```
*Ralph commands*
`/ralph status`          - schedule window, run budget, failure streak, STOP state
`/ralph list`            - the queue, in the order Ralph will work it
`/ralph bump NIK-123`    - make a ticket the next pick (sets Urgent in Linear)
`/ralph skip NIK-123`    - park a ticket in Backlog; Ralph ignores it
`/ralph unskip NIK-123`  - return a parked ticket to Todo
`/ralph stop`            - pause the loop (writes STOP; ticks become no-ops)
`/ralph go`              - resume (clears STOP and the failure streak)
`/ralph help`            - this message
```

`bump` sets Linear priority to Urgent; `skip` moves the ticket to Backlog,
which the gate does not treat as queued. Both are visible in Linear, and both
are reversible from Slack.

### Gate exit codes

`0` work found (Task JSON on stdout) · `10` nothing eligible · `11` outside the
schedule window · `12` per-day cap reached · `13` STOP present · `1` error.
A no-op is exit 0 from `dispatch.sh`; only a broken tick is non-zero, so Task
Scheduler does not report routine quiet ticks as failures.

## The most important finding so far: `--allowedTools` can be decorative

Verified empirically on 2026-09-01, because plan sec.4 ("scoped tools only, never
blanket shell") depends on it and sec.10.4 says not to assume:

| Configuration | Out-of-scope `echo > file` | Verdict |
|---|---|---|
| `--permission-mode acceptEdits` + `--allowedTools "Bash(git status:*)"` | **succeeded** | allowlist ignored |
| `--permission-mode manual` + same allowlist | **blocked** | allowlist enforced |
| `--permission-mode manual` + `--allowedTools Write "Bash(git status:*)"` | both allowed tools worked | correct |

`acceptEdits` is the setting most people would reach for in headless mode, and it
silently voids the entire scoped-tools guardrail while still *looking* correctly
configured. The loop therefore runs in `manual`, and `config.py` now refuses to
load a config using `acceptEdits`, `bypassPermissions`, or `dontAsk`.

**Corollary — do not trust the workspace.** The target repo *commits*
`.claude/settings.local.json`, granting `Bash(pnpm exec *)` (near-arbitrary
execution). Those entries are ignored only because the clone is untrusted. Two
defences: never run Claude interactively in `REPO_DIR`, and `finalize.py` rejects
any run that modifies `.claude/` — otherwise the agent could widen its own
permissions in its own branch.

## Phase 2 shape: the agent cannot push, PR, merge, or deploy

Plan sec.6 has the agent open its own PR and move its own ticket. This build does
neither, for two reasons: sec.10.3 names headless MCP OAuth as *the* risk (and
sanctions "scoped Bash + REST" as the fallback), and capabilities the agent never
has cannot be misused.

```
iterate.sh   workspace prep -> ticket to In Progress -> guarded `claude -p` -> finalize
                                                         (timeout = the only
                                                          enforced bound)
finalize.py  parse report -> reject forbidden edits -> require real commits
             -> git push (credential helper) -> open PR -> ticket to In Review
```

`ralph/github.py` contains no merge function at all. The agent's own report cannot
assert its ticket, branch, or PR URL — `finalize.py` overwrites all three.

## The loop produced a real, verified fix

First productive end-to-end run, 2026-09-02, on NIK-110:

```
gate (NIK-110) -> triage 2s: run=true tier=claude -> claude 9min
   -> 1 commit, 1 file -> status in_review
```

The agent found the ticket's literal scope was already fixed (NIK-102 bumped
@testing-library/react incidentally), then fixed a *different* blocker it found
while verifying: `vi.restoreAllMocks()` in `afterEach` also resets plain
`vi.fn()` mocks, so the module-level `PostHog` mock reverted to returning `{}`
after the first test.

Verified independently, same `node_modules`, only the commit differing:

| | Test files | Tests |
|---|---|---|
| `origin/main` | 1 failed / 7 passed (8) | **4 failed** / 37 passed (41) |
| `ralph/NIK-110` | 0 failed / 8 passed (8) | **41 passed** (41) |

The fix is preserved at `artifacts/NIK-110-logger-mock-fix.patch`, because
`workspace.py` hard-resets the branch on the next run and would destroy it.
Nothing was pushed - there is still no `GITHUB_TOKEN`.

## STOP: the target repo's test suite is already red

`pnpm exec vitest run` on **pristine `origin/main`** fails: 4 tests in
`__tests__/lib/observability/logger.test.ts` (`TypeError: c.capture is not a
function`), plus the component-test failures tracked in NIK-110.

This matters more than any remaining feature. The agent is instructed never to
report success on a red suite, and `finalize.py` refuses to publish a run that
did not commit. So **until the suite is green, essentially every run will
correctly come back `blocked`** - the pipeline working exactly as designed, on a
repo it cannot succeed in.

The agent has now fixed exactly this (above), but the fix is unpushed, so the
baseline is still red. Land it before enabling the schedule. Do not "fix"
this by relaxing the test gate: a loop that opens PRs against a red baseline
produces reviewable-looking work with no signal behind it.

## Phase 6: gpt-oss is a reasoning model, and that breaks the obvious call

Verified on this machine, in this order:

| Call | Result |
|---|---|
| `/api/generate` + `format:"json"` | returns **chain-of-thought prose**, not JSON |
| `/api/generate` + JSON schema | `response` is **empty** (`eval_count: 6`, tokens go to the reasoning channel) |
| `/api/chat` + JSON schema | `message.content` is clean JSON; reasoning lands in `message.thinking` |

So the client uses `/api/chat` with a schema. Warm latency is ~2s per triage.

The server log also confirms plan sec.10.6's trap directly:
`total_vram="16.0 GiB" default_num_ctx=4096`. `num_ctx` is always sent explicitly.

**The pre-filter fails open.** If Ollama is down, returns garbage, or answers
below `min_confidence`, the ticket still goes to Claude. Saving a turn must never
cost a ticket, so only a *confident* skip is honoured.

## Phase 3: proving a preview is non-public, not assuming it

The project setting says `ssoProtection: enabled, all_except_custom_domains`.
That is a claim about configuration, not about a URL, so `protection_of()` takes
no credentials at all and simply fetches the URL as an anonymous visitor would.

The distinction matters here: every deployment returns **302**, so a status-code
check would pass on a public app that merely redirects to its own `/sign-in`.
The real signature is `Location: https://vercel.com/sso-api?...` plus a
`_vercel_sso_nonce` cookie. Confirmed live against this project's previews.

Verified live 2026-09-02: `find_preview` returns the branch's READY deployment,
a bogus token gives a clean `HTTP 403`, and a branch with no deployment yields
an empty URL with a stated reason rather than a wrong one.

If a preview ever turns out to be reachable anonymously, `finalize.py`
**withholds the URL** rather than reporting it: handing out an open link to
unreviewed work is worse than reporting no link.

## Phase 7: the breaker

Three consecutive unproductive (error / no-commit) runs write `STOP`, and only a
human clears it. Verified live end-to-end: 3 failures -> STOP written -> `gate.py`
exits 13 -> `dispatch.sh` no-ops cleanly. A productive run resets the streak, so
two failures and a PR does not leave the loop one failure from stopping.

`python runs.py streak` shows the current count; `prune` applies
`log_retention_days` and runs once per tick.

## A blocked run must de-queue itself

The first real run found NIK-104 was **already done** (commit 76fe35c, merged) and
correctly refused to invent a change. That exposed a design bug worth recording:

- Leaving the ticket in `In Progress` strands it. The gate only selects unstarted
  tickets, so it would never be retried, while the board claims someone is on it.
- Moving it back to `Todo` and nothing else is worse: it gets re-selected on the
  very next tick and spends another Claude turn reaching the same conclusion,
  every tick, forever.

So `finalize.py` now comments the reason on the ticket, removes the
`autonomous-eligible` label, and returns it to `Todo`. Re-queuing is a deliberate
human act. Verified live: after de-queue the gate reports "nothing eligible".

## Bugs this build hit, all silent

Recorded because none of them announced themselves:

1. **The dispatch lock was never released.** `release_lock` used `rmdir`, but a
   `pid` file lived *inside* the lock dir, so it was never empty. Every tick after
   the first would skip for two hours until the stale-lock timeout -- a dead
   schedule that logs nothing alarming. The pid file is now a sibling.
2. **`to_posix` was called but never defined** in `iterate.sh` (only `to_win` was
   copied over). `bash -n` passes, because an undefined function is a *runtime*
   failure -- it surfaced mid-run, after a real Claude turn was spent.
   `tests/test_scripts.py` now catches this class statically.
3. **Claude ran in the wrong directory.** Its sandbox follows its cwd, so invoked
   from the ralph project it could read ralph's own source but not the repo it was
   meant to change. It correctly reported an error having touched nothing -- but a
   less careful prompt could have reported success on an empty change.
4. **The report contract contradicted the prompt.** `in_review` required a
   `pr_url`, while the prompt tells the agent to leave it empty because the
   *wrapper* opens the PR. Validation is now two-stage: lenient for the agent's
   report, strict for the final one handed to Slack.

## Two Windows-specific traps, both fixed

Recording these because they were silent failures, not loud ones:

1. **Git Bash and Windows Python disagree about paths.** `dispatch.sh` is bash,
   `gate.py` is native Windows Python; bash's `/tmp/x` is `C:/tmp/x` to Python.
   They share `RALPH_STATE_DIR`, so unconverted they use *different* state
   directories and the STOP file and run cap silently stop working. `dispatch.sh`
   now normalises with `cygpath` at the boundary.
2. **Ticket titles crash the log.** Linear titles contain em dashes (NIK-106
   does). Redirected to a log file under a cp1252 console, that raises
   `UnicodeEncodeError` and kills the tick over a dash in a title. Every entry
   point now calls `configure_stdio()`.
