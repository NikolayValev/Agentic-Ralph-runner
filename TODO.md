# TODO

## Blocked on you (one command)

Register the Slack listener as a logon-triggered task. Claude Code's auto-mode
classifier refuses to register a logon-persistence task, so this has to be run
by hand:

```powershell
powershell -ExecutionPolicy Bypass -File "C:\Users\Nikolay\code\ralph\install-listener.ps1"
Start-ScheduledTask -TaskName "Ralph listener"
```

Until this runs, `/ralph list`, `/ralph stop` and the Bump/Skip buttons have
nothing listening. Outbound run reports do NOT depend on it — those are posted
by the tick itself.

Undo with `install-listener.ps1 -Remove`. Logs land in `logs/listener-<date>.log`.

## Postponed

- **The loop is STOPPED.** `state/STOP` was written on 2026-09-03 to hold the
  first real run. Resume with `python runs.py stop` → `python runs.py go`, or
  `/ralph go` once the listener is up.
- **NIK-105 is queued and waiting** — labelled `autonomous-eligible`, moved to
  Todo. `gate.py` selects it. It is a two-line fix to `app/privacy/page.tsx`
  whose correct answer is written in the ticket, chosen so the first
  unsupervised run is easy to judge from the preview deploy.

## Still unproven (needs one real run)

- A full tick end to end on the current code: gate → agent → finalize → PR →
  **notify**. The notify call was only wired in on 2026-09-03; its plumbing is
  unit-tested and was verified in isolation, but no live tick has exercised it.
- The Slack buttons and slash commands against real Slack. Socket Mode connects
  (`is_connected() == True`) and the handlers are covered by 232 tests, but
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
