# Slack queue listing and prioritization

Date: 2026-09-02
Status: approved design, not yet implemented

## Problem

`select_ticket` picks the oldest eligible ticket, always. FIFO on `createdAt`
with the identifier as a tiebreak. There is no way to say "do the haptics one
next" short of editing ticket timestamps, and no way to see what Ralph is about
to pick without reading `gate.py --explain` output on the machine itself.

Two capabilities are missing:

1. **Visibility** — what is in the queue, and in what order will it be worked.
2. **Control** — reorder that queue, and park a ticket the bot should leave
   alone for now, from Slack.

## Decisions

These were settled during brainstorming and are not open for re-litigation
during implementation:

| Decision | Choice | Rejected alternative |
|---|---|---|
| Interaction | Deterministic commands + buttons first; NL conversation layered on the same handlers second | NL-only (nothing to fall back to when the parse is wrong) |
| Priority source of truth | Linear's native `priority` field | Ralph-local order file; a `ralph:next` label |
| Skip semantics | Move the ticket to Backlog | Set priority Low; local snooze with expiry; remove the eligible label |
| NL model | Ollama (`gpt-oss:20b`, already configured) | Claude — conversation must never consume the Pro quota the loop depends on |

### Why Linear's priority field

One source of truth, visible in both Linear and Slack, survives a wiped
`state/` directory, and reorderable from the Linear mobile app without Slack.

The accepted cost: **Ralph now writes to Linear tickets**, and a priority set
for human reasons also steers the bot. That coupling is deliberate — the
alternative was two separate notions of "important" that drift apart.

### Why Backlog for skip

`ralph/linear.py` already encodes this: `ELIGIBLE_STATE_TYPES = ("unstarted",)`,
with the comment that a ticket "in Backlog is therefore parked, not queued."
Skip reuses that existing concept and the existing `move_issue` write. No new
vocabulary, no new state, and the reason a ticket left the queue is legible in
Linear rather than only in Ralph's head.

## Phase 1 — commands and buttons

### Ranking

`_QUERY` and `_normalize` in `ralph/linear.py` gain Linear's `priority` field.
`select_ticket`'s sort key becomes:

```
(rank, created_at, identifier)   where   rank = priority or 5
```

**Linear uses `0` for *No priority*, and `1` for *Urgent*.** A naive numeric
sort therefore puts unprioritized tickets first — the exact opposite of intent.
Mapping `0 → 5` sinks them below Low. This is the single most likely bug in the
change and must have a dedicated regression test.

`select_ticket` stays pure and keeps returning `(ticket_or_None, skip_reasons)`.
Determinism is preserved: the identifier remains the final tiebreak.

### New Linear write

```python
def set_priority(api_key: str, issue_key: str, priority: int,
                 *, endpoint: str = ENDPOINT) -> None
```

An `issueUpdate` mutation, sitting beside `move_issue` in the "writes performed
by the wrapper, not the agent" section. Priority is validated against `0..4`
before the call; anything else raises `LinearError` rather than being sent.

Skip and unskip need no new Linear code — they are `move_issue` to the Backlog
state and back to `todo_state`. The Backlog state name comes from config
(`linear.backlog_state`, defaulting to `"Backlog"`), consistent with how
`todo_state`, `in_progress_state` and `review_state` are already declared.

### Handlers

Four additions to `ralph/commands.py`, all returning `Reply` like the existing
handlers, all transport-free so they are directly testable:

| Handler | Command | Effect |
|---|---|---|
| `handle_list` | `/ralph list` | Fetch, rank, render the queue |
| `handle_bump` | `/ralph bump NIK-111` | `set_priority(..., 1)` — Urgent |
| `handle_skip` | `/ralph skip NIK-115` | `move_issue(..., backlog_state)` |
| `handle_unskip` | `/ralph unskip NIK-115` | `move_issue(..., todo_state)` |

`HELP` is extended to cover them. `handle_slash` dispatches on the first word
and passes the remainder as the ticket argument; a missing or malformed
identifier returns usage text rather than raising.

### Rendering

`/ralph list` shows eligible tickets in pick order, marking the next pick, plus
a short tail of skipped tickets with the reason `is_eligible` already produces.
Each eligible row carries Bump and Skip buttons.

Two new action ids in `ralph/slack.py`, following `ACTION_APPROVE`/
`ACTION_DISCARD` exactly — `ACTION_BUMP = "ralph_bump"`, `ACTION_SKIP =
"ralph_skip"`, ticket in the button `value`, `block_id` of the form
`ralph_queue::<ticket>`. `slack_listener.dispatch_action` gains the two cases;
`ticket_from_payload` already handles extraction and needs no change.

Buttons are **not** styled `danger`. Skip is reversible, unlike Discard.

## Phase 2 — natural language

Layered on phase 1's handlers once they are proven. The NL layer holds no
privileges of its own: it can only propose one of the actions above.

### Surface

Direct message to the bot. Socket Mode already delivers events, so this needs
no new infrastructure, but it does need:

- `im:history` and `im:read` scopes, a Message Tab enable, and
  **an app reinstall — meaning a third `xoxb-` token swap.**
- `message.im` event subscription.

### Parsing

`parse_intent` mirrors the existing `parse_verdict` in `ralph/ollama.py`:
strict JSON, fail closed on anything unparseable.

```json
{"action": "bump|skip|unskip|list|unknown",
 "ticket": "NIK-111",
 "confidence": 0.0}
```

Below `local.min_confidence` (already `0.6` in `config.yaml`), or `action` is
`unknown`, the bot asks a clarifying question instead of acting. The prompt is
given the current queue so it can resolve "the haptics one" to an identifier
rather than hallucinating one.

### Confirmation

**Every write is confirmed with buttons before it happens.** The model
proposes; you commit. This is what makes an unreliable 20B model acceptable in
front of a ticket tracker: the worst case is the wrong button offered, not the
wrong ticket parked.

## Error handling

| Condition | Behaviour |
|---|---|
| Linear unreachable / key rejected | Report the error, write nothing. No partial state. |
| Unknown or malformed ticket id | Offer near matches from the queue already fetched |
| Low-confidence parse | Ask a clarifying question; never guess |
| Stale confirmation (queue changed since the proposal) | Re-check before writing; report the drift instead of applying blind |
| Ollama down | NL replies degrade to "use `/ralph list`"; phase 1 is unaffected |

The listener already swallows handler exceptions so one bad interaction cannot
kill the socket. That behaviour is preserved.

## Testing

Following existing conventions — `monkeypatch`, no HTTP mocking library, no
live Slack or Linear in the suite.

- **Ranking**: table-driven, in the style of `test_gate.py`. Must cover priority
  0 sorting last, equal priorities falling back to `created_at`, and the
  identifier tiebreak.
- **`parse_intent`**: fixture strings including malformed JSON, prose-wrapped
  JSON, and sub-threshold confidence.
- **Handlers**: `monkeypatch`ed `set_priority` / `move_issue`, asserting both
  the reply text and that the right write was attempted with the right
  arguments.
- **`set_priority`**: rejects out-of-range values without issuing a request.

## Out of scope

- Merging. `handle_approve` stays a stub; Ralph does not merge, by design.
- Reordering beyond "make this next" — no drag-to-rank, no numeric positions.
- Changing which tickets are eligible. Label rules are untouched.
- Scout mode. Still off.

## Notes

`C:\Users\Nikolay\code\ralph` is not a git repository, so this spec cannot be
committed as the brainstorming workflow would normally require. It lives on
disk only.
