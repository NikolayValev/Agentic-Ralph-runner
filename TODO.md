# TODO

## Blocked on you: Slack scopes for plain-English DMs

Tasks 1-3 of the natural-language plan need none of this. Task 4 (routing the
DM itself) is written but cannot be verified live until this is done.

In https://api.slack.com/apps -> your Ralph app:

1. **OAuth & Permissions -> Bot Token Scopes** -> add both:
   - `im:history`  (read DM content)
   - `im:read`     (see DM conversations)
2. **App Home -> Show Tabs -> Messages Tab** -> enable it, and tick
   "Allow users to send Slash commands and messages from the messages tab".
3. **Event Subscriptions -> Subscribe to bot events** -> add `message.im`.
   (Socket Mode is already on, so no Request URL is needed.)
4. **Reinstall the app.** This issues a NEW `xoxb-` token.
5. Replace `SLACK_BOT_TOKEN` in `.env` with the new token, then:
   `Restart-ScheduledTask -TaskName "Ralph listener"`

Verify from Git Bash:

```bash
set -a; source .env; set +a
curl -s -D - -o /dev/null -X POST https://slack.com/api/auth.test   -H "Authorization: Bearer $SLACK_BOT_TOKEN" | grep -i x-oauth-scopes
```

`im:history` and `im:read` must both appear. Until step 5, the old token is
dead and the listener will fail to post -- so do 4 and 5 together.

## Done

- [x] ~~Register the Slack listener as a logon task~~ - installed 2026-09-04;
  task "Ralph listener" is Running and survives reboots.

## Postponed

- **The loop is STOPPED.** `state/STOP` was written on 2026-09-03 to hold the
  first real run. Resume with `python runs.py go`, or
  `/ralph go` once the listener is up.
- **NIK-105 is queued and waiting** — labelled `autonomous-eligible`, moved to
  Todo. `gate.py` selects it. It is a two-line fix to `app/privacy/page.tsx`
  whose correct answer is written in the ticket, chosen so the first
  unsupervised run is easy to judge from the preview deploy.

## Verified ready (2026-09-03)

Checked rather than assumed, before the first real run:

- Target repo baseline on `origin/main`: install ok, **40/40 tests pass**,
  lint 0 errors. This was the README's stated hard blocker; NIK-110's fix
  merged as `f9d1dce` cleared it.
- `preflight.py`: 0 failures, 0 warnings.
- Local triage on NIK-105 returns `run=true tier=claude` with a sensible
  commit hint, so the ticket will not be silently dropped at tier 1.
- The agent prompt for NIK-105 renders at 3509 chars with no unsubstituted
  placeholders.
- Workspace trust flag still `true` in `~/.claude.json`, so the agent's
  allowlist actually applies instead of being ignored.
- `dispatch.sh` rehearsed end to end under STOP: no-op, lock released, no run
  consumed.
- Ollama serving `gpt-oss:20b`; Slack both tokens valid; Vercel preview
  protected; sleep disabled.

## Still unproven (needs one real run)

- A full tick end to end on the current code: gate → agent → finalize → PR →
  **notify**. The notify call was only wired in on 2026-09-03; its plumbing is
  unit-tested and was verified in isolation, but no live tick has exercised it.
- The Slack buttons and slash commands against real Slack. Socket Mode connects
  (`is_connected() == True`) and the handlers are covered by tests, but
  nobody has pressed Bump in a real channel.

## Follow-ups (not blocking)

- `build_message` interpolates `report.summary` into mrkdwn unescaped — the same
  bug fixed for queue rows in `build_queue_blocks`. Same React-component titles
  will garble it.
- Bump is sticky: nothing clears Urgent after a ticket ships.
- A button press carrying a malformed `block_id` yields an ephemeral reply with
  no user, so it posts publicly instead. Leaks a usage string, not data.
- Phase 2 of the queue work (natural language over Slack DM) is specced at
  `docs/superpowers/specs/2026-09-02-slack-queue-prioritization-design.md` but
  has no plan yet. Needs `im:history` + `im:read` scopes and an app reinstall.
- The GitHub token in `.env` is a classic token carrying `admin:org`,
  `delete_repo` and `admin:enterprise`. `finalize.py` uses it for exactly one
  call (`POST /pulls`). A fine-grained token scoped to the one repo with
  `contents:write` + `pull_requests:write` would do the same work.
