# Slack Queue Prioritization Implementation Plan (Phase 1)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human see Ralph's work queue in Slack and reorder it, by making Linear's native priority field the gate's primary sort key.

**Architecture:** `ralph/linear.py` gains a priority-aware `rank_issues` that `select_ticket` delegates to, plus one new write (`set_priority`). `ralph/slack.py` gains a pure Block Kit builder for the queue. `ralph/commands.py` gains four handlers that both `/ralph <cmd>` and the buttons route into. Nothing new is invented for "skip" — it reuses the Backlog state the gate already treats as parked.

**Tech Stack:** Python 3.10+, `requests`, `slack_sdk` (Socket Mode), pytest with `monkeypatch`. No HTTP mocking library — do not add one.

**Spec:** `docs/superpowers/specs/2026-09-02-slack-queue-prioritization-design.md`

## Global Constraints

- **`ANTHROPIC_API_KEY` must never be set**, in code, tests, or fixtures. A present key makes `claude -p` bill per-token instead of using the subscription; `preflight.py` fails loudly on it.
- **No live network in the test suite.** Linear and Slack calls are `monkeypatch`ed. Follow `tests/test_commands.py`.
- **Linear priority encoding: `0`=None, `1`=Urgent, `2`=High, `3`=Medium, `4`=Low.** `0` must sort *last*.
- **Ralph never merges.** Nothing in this plan touches `handle_approve`.
- Message construction stays pure and separate from sending, per the module docstring in `ralph/slack.py` — a malformed payload is otherwise discovered at send time in an unattended run.
- Run the suite with `python -m pytest -q` from `C:\Users\Nikolay\code\ralph`. Baseline is **173 passing**.
- Commit after every task. This repo has no remote; do not add one.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `ralph/linear.py` | Linear fetch, pure selection rules, wrapper writes | Modify — add `priority` to query/normalize, add `priority_rank` + `rank_issues`, add `set_priority` |
| `ralph/slack.py` | Pure Block Kit construction | Modify — add `ACTION_BUMP`/`ACTION_SKIP`, `build_queue_blocks`, `queue_headline` |
| `ralph/commands.py` | Transport-free command behaviour | Modify — `Reply.blocks`, four handlers, dispatch, `HELP` |
| `slack_listener.py` | Socket Mode transport | Modify — route the two new actions, post blocks |
| `config.yaml` | Policy | Modify — add `linear.backlog_state` |
| `tests/test_queue_ranking.py` | Ranking rules | Create |
| `tests/test_linear_priority.py` | The `set_priority` write | Create |
| `tests/test_queue_message.py` | Block Kit payload | Create |
| `tests/test_commands.py` | Handlers and routing | Modify — append |

---

### Task 1: Priority-aware ranking

**Files:**
- Modify: `ralph/linear.py` (`_QUERY`, `_normalize`, `select_ticket`)
- Test: `tests/test_queue_ranking.py` (create)

**Interfaces:**
- Consumes: existing `is_eligible(issue, *, eligible_label, repo_label) -> tuple[bool, str]`
- Produces:
  - `priority_rank(issue: dict) -> int`
  - `rank_issues(issues, *, eligible_label: str, repo_label: str) -> tuple[list[dict], list[str]]`
  - `select_ticket` keeps its existing signature and return shape
  - Constants `URGENT = 1`, `NO_PRIORITY = 0`, `NO_PRIORITY_RANK = 5`, `VALID_PRIORITIES = (0, 1, 2, 3, 4)`
  - Normalized issue dicts gain an `int` key `"priority"`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_ranking.py`:

```python
"""Ranking: Linear priority first, then oldest-first.

The trap this file exists to guard: Linear encodes 0 as "No priority" and 1 as
"Urgent", so sorting on the raw value puts unprioritized tickets ahead of
urgent ones -- the exact opposite of what a human means by "prioritized".
"""

from __future__ import annotations

from ralph.linear import (NO_PRIORITY_RANK, URGENT, priority_rank, rank_issues,
                          select_ticket)

ELIGIBLE = "autonomous-eligible"
REPO = "repo:quitting-smoking-tracker"


def issue(identifier, *, priority=0, created_at="2026-01-01T00:00:00Z",
          labels=None, state_type="unstarted"):
    """A normalized issue, in the shape _normalize produces."""
    return {
        "id": f"uuid-{identifier}",
        "identifier": identifier,
        "title": f"title for {identifier}",
        "created_at": created_at,
        "state_name": "Todo",
        "state_type": state_type,
        "labels": [ELIGIBLE, REPO] if labels is None else labels,
        "priority": priority,
    }


def order(issues):
    ranked, _ = rank_issues(issues, eligible_label=ELIGIBLE, repo_label=REPO)
    return [i["identifier"] for i in ranked]


def test_no_priority_ranks_below_low():
    assert priority_rank({"priority": 0}) == NO_PRIORITY_RANK
    assert priority_rank({"priority": 4}) == 4
    assert priority_rank({"priority": URGENT}) == 1


def test_missing_priority_key_is_treated_as_no_priority():
    """Issues from a fixture file or an older cache may not carry the field."""
    assert priority_rank({}) == NO_PRIORITY_RANK
    assert priority_rank({"priority": None}) == NO_PRIORITY_RANK


def test_unprioritized_sorts_last_not_first():
    assert order([issue("NIK-1", priority=0), issue("NIK-2", priority=4)]) == ["NIK-2", "NIK-1"]


def test_urgent_beats_older_unprioritized():
    """The whole point of the feature: a bump wins over age."""
    old = issue("NIK-1", priority=0, created_at="2020-01-01T00:00:00Z")
    bumped = issue("NIK-2", priority=URGENT, created_at="2026-06-01T00:00:00Z")
    assert order([old, bumped]) == ["NIK-2", "NIK-1"]


def test_equal_priority_falls_back_to_oldest_first():
    newer = issue("NIK-1", priority=2, created_at="2026-06-01T00:00:00Z")
    older = issue("NIK-2", priority=2, created_at="2026-01-01T00:00:00Z")
    assert order([newer, older]) == ["NIK-2", "NIK-1"]


def test_identical_priority_and_age_falls_back_to_identifier():
    """Determinism across runs matters: the gate must not flap."""
    assert order([issue("NIK-9"), issue("NIK-2")]) == ["NIK-2", "NIK-9"]


def test_ineligible_issues_are_excluded_with_reasons():
    ranked, skipped = rank_issues(
        [issue("NIK-1"), issue("NIK-2", state_type="backlog")],
        eligible_label=ELIGIBLE, repo_label=REPO)
    assert [i["identifier"] for i in ranked] == ["NIK-1"]
    assert len(skipped) == 1 and "NIK-2" in skipped[0]


def test_select_ticket_returns_the_head_of_the_ranking():
    head, skipped = select_ticket(
        [issue("NIK-1", priority=0), issue("NIK-2", priority=URGENT)],
        eligible_label=ELIGIBLE, repo_label=REPO)
    assert head["identifier"] == "NIK-2"
    assert skipped == []


def test_select_ticket_returns_none_when_nothing_is_eligible():
    head, skipped = select_ticket(
        [issue("NIK-1", state_type="backlog")],
        eligible_label=ELIGIBLE, repo_label=REPO)
    assert head is None and len(skipped) == 1
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_queue_ranking.py -q`
Expected: FAIL — `ImportError: cannot import name 'NO_PRIORITY_RANK' from 'ralph.linear'`

- [ ] **Step 3: Add `priority` to the query and the normalizer**

In `ralph/linear.py`, add `priority` to the `_QUERY` node selection, immediately after `title`:

```
    nodes {
      id
      identifier
      title
      priority
      createdAt
      state { name type }
      labels { nodes { name } }
    }
```

And add the field to `_normalize`, after `"title"`:

```python
        "priority": int(node.get("priority") or 0),
```

- [ ] **Step 4: Add the ranking functions**

In `ralph/linear.py`, in the "pure selection rules" section, directly above `select_ticket`:

```python
# Linear encodes priority as 0=None, 1=Urgent, 2=High, 3=Medium, 4=Low. Sorting
# on the raw value would rank UNPRIORITIZED tickets above Urgent ones, so 0 is
# remapped below Low rather than compared numerically.
NO_PRIORITY = 0
URGENT = 1
NO_PRIORITY_RANK = 5
VALID_PRIORITIES = (0, 1, 2, 3, 4)


def priority_rank(issue: dict) -> int:
    """Sort position for a ticket's priority: lower is worked sooner."""
    priority = int(issue.get("priority") or NO_PRIORITY)
    return NO_PRIORITY_RANK if priority == NO_PRIORITY else priority


def rank_issues(
    issues: Iterable[dict], *, eligible_label: str, repo_label: str
) -> tuple[list[dict], list[str]]:
    """Every eligible ticket in the order the gate will work them.

    Returns (ranked, skip_reasons). Priority first, then oldest-first, then the
    identifier -- so the ordering is total and stable across runs.
    """
    eligible: list[dict] = []
    skipped: list[str] = []
    for issue in issues:
        ok, reason = is_eligible(
            issue, eligible_label=eligible_label, repo_label=repo_label
        )
        if ok:
            eligible.append(issue)
        else:
            skipped.append(f"{issue.get('identifier', '?')}: {reason}")
    eligible.sort(key=lambda i: (
        priority_rank(i), i.get("created_at") or "", i.get("identifier") or ""))
    return eligible, skipped
```

- [ ] **Step 5: Reduce `select_ticket` to the head of the ranking**

Replace the body of `select_ticket` in `ralph/linear.py`, keeping its signature and docstring intent:

```python
def select_ticket(
    issues: Iterable[dict], *, eligible_label: str, repo_label: str
) -> tuple[dict | None, list[str]]:
    """Pick the ticket the next tick should work.

    Thin wrapper over rank_issues so the gate and `/ralph list` can never
    disagree about what comes next.
    """
    ranked, skipped = rank_issues(
        issues, eligible_label=eligible_label, repo_label=repo_label
    )
    return (ranked[0] if ranked else None), skipped
```

- [ ] **Step 6: Run the new tests**

Run: `python -m pytest tests/test_queue_ranking.py -q`
Expected: PASS, 9 passed

- [ ] **Step 7: Run the whole suite for regressions**

Run: `python -m pytest -q`
Expected: PASS — 182 passed (173 baseline + 9). `tests/test_gate.py` must be green: its existing oldest-first cases still hold because fixtures carry no priority, which now ranks 5 uniformly.

- [ ] **Step 8: Commit**

```bash
git add ralph/linear.py tests/test_queue_ranking.py
git commit -m "feat: rank the queue by Linear priority, then age"
```

---

### Task 2: The `set_priority` write

**Files:**
- Modify: `ralph/linear.py` (writes section, after `move_issue`)
- Test: `tests/test_linear_priority.py` (create)

**Interfaces:**
- Consumes: `_post(api_key, query, variables, endpoint) -> dict`, `VALID_PRIORITIES` and `URGENT` from Task 1
- Produces: `set_priority(api_key: str, issue_key: str, priority: int, *, endpoint: str = ENDPOINT) -> int` — returns the priority Linear confirms

- [ ] **Step 1: Write the failing tests**

Create `tests/test_linear_priority.py`:

```python
"""The one new Linear write. Validation happens before the request, not after."""

from __future__ import annotations

import pytest

import ralph.linear as linear
from ralph.linear import URGENT, LinearError, set_priority


@pytest.fixture
def no_network(monkeypatch):
    """Fail loudly if a test reaches _post when it should not have."""
    def explode(*args, **kwargs):
        raise AssertionError("_post was called; validation should have stopped it")
    monkeypatch.setattr(linear, "_post", explode)


@pytest.mark.parametrize("bad", [-1, 5, 99, "urgent", None])
def test_invalid_priority_is_rejected_without_a_request(no_network, bad):
    with pytest.raises(LinearError, match="priority"):
        set_priority("key", "NIK-1", bad)


def test_valid_priority_is_sent_and_confirmed(monkeypatch):
    seen = {}

    def fake_post(api_key, query, variables, endpoint=linear.ENDPOINT):
        seen["variables"] = variables
        return {"issueUpdate": {"success": True,
                                "issue": {"identifier": "NIK-1", "priority": URGENT}}}

    monkeypatch.setattr(linear, "_post", fake_post)
    assert set_priority("key", "NIK-1", URGENT) == URGENT
    assert seen["variables"] == {"id": "NIK-1", "priority": URGENT}


def test_a_refused_update_raises(monkeypatch):
    monkeypatch.setattr(
        linear, "_post",
        lambda *a, **k: {"issueUpdate": {"success": False, "issue": None}})
    with pytest.raises(LinearError, match="refused"):
        set_priority("key", "NIK-1", URGENT)
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_linear_priority.py -q`
Expected: FAIL — `ImportError: cannot import name 'set_priority' from 'ralph.linear'`

- [ ] **Step 3: Add the mutation and the function**

In `ralph/linear.py`, after `move_issue`:

```python
_PRIORITY_MUTATION = """
mutation SetPriority($id: String!, $priority: Int!) {
  issueUpdate(id: $id, input: { priority: $priority }) {
    success
    issue { identifier priority }
  }
}
"""


def set_priority(
    api_key: str, issue_key: str, priority: int, *, endpoint: str = ENDPOINT
) -> int:
    """Set a ticket's Linear priority. Returns the priority Linear confirms.

    Validated before the request rather than relying on Linear to reject it: an
    out-of-range value is a bug in our caller, and a rejected mutation would
    surface as an opaque GraphQL error in an unattended run.
    """
    if priority not in VALID_PRIORITIES:
        raise LinearError(
            f"priority must be one of {VALID_PRIORITIES} "
            f"(0=None, 1=Urgent, 4=Low), got {priority!r}")
    data = _post(api_key, _PRIORITY_MUTATION,
                 {"id": issue_key, "priority": priority}, endpoint)
    result = data["issueUpdate"]
    if not result.get("success"):
        raise LinearError(f"Linear refused to set priority on {issue_key}")
    return int(result["issue"]["priority"])
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_linear_priority.py -q`
Expected: PASS, 7 passed

- [ ] **Step 5: Commit**

```bash
git add ralph/linear.py tests/test_linear_priority.py
git commit -m "feat: add set_priority write to the Linear client"
```

---

### Task 3: The queue Block Kit payload

**Files:**
- Modify: `ralph/slack.py`
- Test: `tests/test_queue_message.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks at runtime — takes plain issue dicts, so `slack.py` stays independent of `linear.py`
- Produces:
  - `ACTION_BUMP = "ralph_bump"`, `ACTION_SKIP = "ralph_skip"`
  - `QUEUE_ROW_LIMIT = 10`
  - `PRIORITY_LABEL: dict[int, str]`
  - `queue_headline(ranked: list[dict]) -> str`
  - `build_queue_blocks(ranked: list[dict], skipped: list[str]) -> list[dict]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_queue_message.py`:

```python
"""The queue payload is built without a token, so it can be checked exhaustively."""

from __future__ import annotations

from ralph.slack import (ACTION_BUMP, ACTION_SKIP, QUEUE_ROW_LIMIT,
                         build_queue_blocks, queue_headline)


def issue(identifier, *, priority=0, title="a title"):
    return {"identifier": identifier, "title": title, "priority": priority}


def blocks_of_type(blocks, kind):
    return [b for b in blocks if b["type"] == kind]


def test_headline_names_the_next_pick():
    assert "NIK-1" in queue_headline([issue("NIK-1"), issue("NIK-2")])


def test_headline_for_an_empty_queue_says_so():
    assert "nothing eligible" in queue_headline([]).lower()


def test_every_row_carries_bump_and_skip_for_its_own_ticket():
    blocks = build_queue_blocks([issue("NIK-1"), issue("NIK-2")], [])
    actions = blocks_of_type(blocks, "actions")
    assert len(actions) == 2
    for block, ticket in zip(actions, ["NIK-1", "NIK-2"]):
        assert block["block_id"] == f"ralph_queue::{ticket}"
        ids = [e["action_id"] for e in block["elements"]]
        assert ids == [ACTION_BUMP, ACTION_SKIP]
        assert all(e["value"] == ticket for e in block["elements"])


def test_skip_is_not_styled_as_destructive():
    """Skip parks a ticket and is reversible, unlike Discard."""
    blocks = build_queue_blocks([issue("NIK-1")], [])
    for element in blocks_of_type(blocks, "actions")[0]["elements"]:
        assert "style" not in element or element["style"] != "danger"


def test_priority_is_shown_by_name_not_number():
    blocks = build_queue_blocks([issue("NIK-1", priority=1)], [])
    assert "Urgent" in blocks[1]["text"]["text"]


def test_long_queues_are_truncated_with_a_count():
    many = [issue(f"NIK-{n}") for n in range(QUEUE_ROW_LIMIT + 5)]
    blocks = build_queue_blocks(many, [])
    assert len(blocks_of_type(blocks, "actions")) == QUEUE_ROW_LIMIT
    assert "5 more" in blocks[-1]["elements"][0]["text"]


def test_skipped_tickets_appear_as_context_not_rows():
    blocks = build_queue_blocks([issue("NIK-1")], ["NIK-9: missing 'repo:x'"])
    assert len(blocks_of_type(blocks, "actions")) == 1
    assert "NIK-9" in blocks[-1]["elements"][0]["text"]


def test_an_empty_queue_still_produces_a_valid_payload():
    blocks = build_queue_blocks([], [])
    assert blocks and blocks[0]["type"] == "section"
    assert blocks_of_type(blocks, "actions") == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_queue_message.py -q`
Expected: FAIL — `ImportError: cannot import name 'ACTION_BUMP' from 'ralph.slack'`

- [ ] **Step 3: Add the constants**

In `ralph/slack.py`, beside the existing action ids:

```python
ACTION_BUMP = "ralph_bump"
ACTION_SKIP = "ralph_skip"

# Slack caps a message at 50 blocks and each row costs two. Ten rows is plenty
# to choose from and leaves headroom for the header and context lines.
QUEUE_ROW_LIMIT = 10

PRIORITY_LABEL = {0: "None", 1: "Urgent", 2: "High", 3: "Medium", 4: "Low"}
```

- [ ] **Step 4: Add the builders**

At the end of `ralph/slack.py`:

```python
def queue_headline(ranked: list[dict]) -> str:
    """One-line summary, also used as the notification fallback text."""
    if not ranked:
        return ":white_circle: Ralph queue - nothing eligible"
    return (f":clipboard: Ralph queue - {len(ranked)} eligible, "
            f"next is {ranked[0]['identifier']}")


def build_queue_blocks(ranked: list[dict], skipped: list[str]) -> list[dict]:
    """Block Kit for `/ralph list`: the pick order, with per-row controls.

    Takes plain issue dicts rather than importing from ralph.linear, so message
    construction stays independent of how the queue was produced.
    """
    blocks: list[dict] = [
        {"type": "section",
         "text": {"type": "mrkdwn", "text": f"*{queue_headline(ranked)}*"}},
    ]

    for position, issue in enumerate(ranked[:QUEUE_ROW_LIMIT], start=1):
        ticket = issue["identifier"]
        marker = ":arrow_forward:" if position == 1 else f"{position}."
        priority = PRIORITY_LABEL.get(int(issue.get("priority") or 0), "None")
        title = (issue.get("title") or "")[:80]
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn",
                     "text": f"{marker}  <{linear_url(ticket)}|{ticket}>  {title}  _{priority}_"},
        })
        blocks.append({
            "type": "actions",
            "block_id": f"ralph_queue::{ticket}",
            "elements": [
                # Neither button is styled `danger`: bump and skip are both
                # reversible, unlike Discard on a run report.
                {"type": "button", "action_id": ACTION_BUMP,
                 "text": {"type": "plain_text", "text": "Bump"}, "value": ticket},
                {"type": "button", "action_id": ACTION_SKIP,
                 "text": {"type": "plain_text", "text": "Skip"}, "value": ticket},
            ],
        })

    hidden = len(ranked) - QUEUE_ROW_LIMIT
    if hidden > 0:
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"_...and {hidden} more_"}]})

    if skipped:
        shown = "  |  ".join(skipped[:5])
        blocks.append({"type": "context", "elements": [
            {"type": "mrkdwn", "text": f"_not queued: {shown}_"[:3000]}]})

    return blocks
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_queue_message.py -q`
Expected: PASS, 8 passed

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 197 passed. `tests/test_notify.py` must still be green; `build_message` was not touched.

- [ ] **Step 7: Commit**

```bash
git add ralph/slack.py tests/test_queue_message.py
git commit -m "feat: build the queue Block Kit payload"
```

---

### Task 4: Command handlers

**Files:**
- Modify: `ralph/commands.py`
- Modify: `config.yaml` (add `linear.backlog_state`)
- Test: `tests/test_commands.py` (append)

**Interfaces:**
- Consumes: `rank_issues`, `set_priority`, `URGENT`, `fetch_labelled_issues`, `move_issue`, `LinearError` from `ralph.linear`; `build_queue_blocks`, `queue_headline` from `ralph.slack`
- Produces:
  - `Reply` gains `blocks: list[dict] | None = None`
  - `normalize_ticket(raw: str) -> str` — `""` when malformed
  - `handle_list(cfg) -> Reply`
  - `handle_bump(cfg, ticket) -> Reply`
  - `handle_skip(cfg, ticket) -> Reply`
  - `handle_unskip(cfg, ticket) -> Reply`
  - `handle_slash` routes `list`/`queue`/`q`, `bump`/`next`, `skip`, `unskip`

- [ ] **Step 1: Add the Backlog state to config**

In `config.yaml`, in the `linear:` section, after `todo_state`:

```yaml
  backlog_state: Backlog           # where /ralph skip parks a ticket
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_commands.py`:

```python
# --- queue listing and prioritization ---------------------------------------

import ralph.linear as linear_mod
from ralph.commands import (handle_bump, handle_list, handle_skip,
                            handle_unskip, normalize_ticket)
from ralph.linear import URGENT, LinearError
from ralph.slack import ACTION_BUMP, ACTION_SKIP


def queue_issue(identifier, *, priority=0):
    return {
        "id": f"uuid-{identifier}", "identifier": identifier,
        "title": f"title for {identifier}", "created_at": "2026-01-01T00:00:00Z",
        "state_name": "Todo", "state_type": "unstarted",
        "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"],
        "priority": priority,
    }


@pytest.fixture
def fake_linear(monkeypatch):
    """Record writes instead of performing them."""
    calls = {"priority": [], "moves": []}
    monkeypatch.setattr(
        commands, "fetch_labelled_issues",
        lambda *a, **k: [queue_issue("NIK-1"), queue_issue("NIK-2", priority=URGENT)])
    monkeypatch.setattr(
        commands, "set_priority",
        lambda key, ticket, priority, **k: calls["priority"].append((ticket, priority)))
    monkeypatch.setattr(
        commands, "move_issue",
        lambda key, ticket, state, team, **k: (calls["moves"].append((ticket, state)), state)[1])
    return calls


@pytest.mark.parametrize("raw,expected", [
    ("NIK-110", "NIK-110"), ("nik-110", "NIK-110"), ("  NIK-110  ", "NIK-110"),
    ("", ""), ("garbage", ""), ("NIK-", ""), ("110", ""),
])
def test_normalize_ticket(raw, expected):
    assert normalize_ticket(raw) == expected


def test_list_ranks_urgent_first_and_attaches_blocks(isolated, cfg, fake_linear):
    reply = handle_list(cfg)
    assert "NIK-2" in reply.text, "the urgent ticket is the next pick"
    assert reply.blocks, "the list is rendered as blocks, not just text"


def test_list_reports_a_linear_outage_without_raising(isolated, cfg, monkeypatch):
    def boom(*a, **k):
        raise LinearError("Linear request failed")
    monkeypatch.setattr(commands, "fetch_labelled_issues", boom)
    reply = handle_list(cfg)
    assert "could not read" in reply.text.lower()


def test_bump_sets_urgent(isolated, cfg, fake_linear):
    reply = handle_bump(cfg, "NIK-1")
    assert fake_linear["priority"] == [("NIK-1", URGENT)]
    assert reply.ephemeral is False


def test_bump_rejects_a_malformed_ticket_without_writing(isolated, cfg, fake_linear):
    reply = handle_bump(cfg, "not-a-ticket")
    assert fake_linear["priority"] == []
    assert "usage" in reply.text.lower()


def test_skip_parks_the_ticket_in_backlog(isolated, cfg, fake_linear):
    handle_skip(cfg, "NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["backlog_state"])]


def test_unskip_returns_it_to_todo(isolated, cfg, fake_linear):
    handle_unskip(cfg, "NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["todo_state"])]


def test_slash_routes_the_new_commands(isolated, cfg, fake_linear):
    assert handle_slash("list", cfg).blocks
    handle_slash("bump NIK-1", cfg)
    assert fake_linear["priority"] == [("NIK-1", URGENT)]


def test_help_documents_the_new_commands(isolated, cfg):
    text = handle_slash("help", cfg).text
    for command in ("list", "bump", "skip", "unskip"):
        assert command in text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -q`
Expected: FAIL — `ImportError: cannot import name 'handle_list' from 'ralph.commands'`

- [ ] **Step 4: Extend the imports and `Reply`**

In `ralph/commands.py`, replace the `ralph.linear` import line and add the slack import:

```python
import re

from ralph.linear import (LinearError, URGENT, fetch_labelled_issues, move_issue,
                          rank_issues, set_priority)
from ralph.slack import build_queue_blocks, queue_headline
```

And extend `Reply`:

```python
@dataclass(frozen=True)
class Reply:
    text: str
    ephemeral: bool = True
    # Block Kit payload for replies that are richer than a line of text. The
    # transport falls back to `text` when this is None.
    blocks: list[dict] | None = None
```

- [ ] **Step 5: Add the handlers**

In `ralph/commands.py`, after `status_text` and before `handle_slash`:

```python
TICKET_RE = re.compile(r"^[A-Za-z]+-\d+$")


def normalize_ticket(raw: str) -> str:
    """Uppercase a ticket id, or return '' if it is not one.

    Slack helpfully wraps bare text in angle brackets when it thinks it is a
    link, so those are stripped before matching.
    """
    candidate = (raw or "").strip().strip("<>").split("|")[0].strip().upper()
    return candidate if TICKET_RE.match(candidate) else ""


def _api_key() -> str:
    return os.environ.get("LINEAR_API_KEY", "")


def handle_list(cfg: Config) -> Reply:
    """Show the queue in the order the gate will work it."""
    try:
        issues = fetch_labelled_issues(
            _api_key(), cfg.linear["team_key"], cfg.linear["eligible_label"])
    except LinearError as exc:
        return Reply(f":warning: could not read the queue: {exc}")

    ranked, skipped = rank_issues(
        issues,
        eligible_label=cfg.linear["eligible_label"],
        repo_label=cfg.linear.get("repo_label", ""))
    return Reply(queue_headline(ranked), blocks=build_queue_blocks(ranked, skipped))


def handle_bump(cfg: Config, ticket: str) -> Reply:
    """Make a ticket the next pick by setting it Urgent in Linear."""
    ticket = normalize_ticket(ticket)
    if not ticket:
        return Reply("Usage: `/ralph bump NIK-123`")
    try:
        set_priority(_api_key(), ticket, URGENT)
    except LinearError as exc:
        return Reply(f":warning: {ticket}: could not bump: {exc}", ephemeral=False)
    return Reply(
        f":arrow_up: {ticket} set to Urgent. It goes next unless something else "
        f"is also Urgent and older.",
        ephemeral=False)


def _move(cfg: Config, ticket: str, state_key: str, default: str,
          icon: str, verb: str, usage: str) -> Reply:
    """Shared body for skip and unskip: both are one move_issue call."""
    ticket = normalize_ticket(ticket)
    if not ticket:
        return Reply(usage)
    state = cfg.linear.get(state_key, default)
    try:
        landed = move_issue(_api_key(), ticket, state, cfg.linear["team_key"])
    except LinearError as exc:
        return Reply(f":warning: {ticket}: could not {verb}: {exc}", ephemeral=False)
    return Reply(f"{icon} {ticket} moved to {landed}.", ephemeral=False)


def handle_skip(cfg: Config, ticket: str) -> Reply:
    """Park a ticket: Backlog is not an eligible state, so the gate ignores it."""
    return _move(cfg, ticket, "backlog_state", "Backlog",
                 ":double_vertical_bar:", "skip", "Usage: `/ralph skip NIK-123`")


def handle_unskip(cfg: Config, ticket: str) -> Reply:
    """Return a parked ticket to the queue."""
    return _move(cfg, ticket, "todo_state", "Todo",
                 ":leftwards_arrow_with_hook:", "unskip",
                 "Usage: `/ralph unskip NIK-123`")
```

- [ ] **Step 6: Route the new commands**

In `ralph/commands.py`, replace the first line of `handle_slash` so the argument survives, and add the branches before the `help` branch:

```python
def handle_slash(text: str, cfg: Config) -> Reply:
    parts = (text or "").strip().split()
    command = parts[0].lower() if parts else "status"
    argument = parts[1] if len(parts) > 1 else ""
```

```python
    if command in ("list", "queue", "q"):
        return handle_list(cfg)
    if command in ("bump", "next"):
        return handle_bump(cfg, argument)
    if command == "skip":
        return handle_skip(cfg, argument)
    if command == "unskip":
        return handle_unskip(cfg, argument)
```

- [ ] **Step 7: Extend `HELP`**

Replace `HELP` in `ralph/commands.py`:

```python
HELP = (
    "*Ralph commands*\n"
    "`/ralph status`          - schedule window, run budget, failure streak, STOP state\n"
    "`/ralph list`            - the queue, in the order Ralph will work it\n"
    "`/ralph bump NIK-123`    - make a ticket the next pick (sets Urgent in Linear)\n"
    "`/ralph skip NIK-123`    - park a ticket in Backlog; Ralph ignores it\n"
    "`/ralph unskip NIK-123`  - return a parked ticket to Todo\n"
    "`/ralph stop`            - pause the loop (writes STOP; ticks become no-ops)\n"
    "`/ralph go`              - resume (clears STOP and the failure streak)\n"
    "`/ralph help`            - this message"
)
```

- [ ] **Step 8: Run the tests**

Run: `python -m pytest tests/test_commands.py -q`
Expected: PASS — existing cases plus 14 new.

- [ ] **Step 9: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 211 passed.

- [ ] **Step 10: Verify against real Linear, read-only**

Run: `bash -c 'set -a; source .env; set +a; python -c "
from ralph.config import load
from ralph.commands import handle_list
print(handle_list(load()).text)"'`
Expected: a real headline naming the ticket the next tick would pick. If it names one you did not expect, stop — the ranking is wrong and Task 1 needs revisiting before any writes happen.

- [ ] **Step 11: Commit**

```bash
git add ralph/commands.py config.yaml tests/test_commands.py
git commit -m "feat: /ralph list, bump, skip and unskip"
```

---

### Task 5: Wire the buttons through the listener

**Files:**
- Modify: `slack_listener.py`
- Modify: `README.md`
- Test: `tests/test_commands.py` (append)

**Interfaces:**
- Consumes: `handle_bump`, `handle_skip` from Task 4; `ACTION_BUMP`, `ACTION_SKIP` from Task 3
- Produces: `dispatch_action` routes the two new ids; `_post_reply(web, channel, reply, user=None)` sends `blocks` when present

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands.py`:

```python
def test_dispatch_routes_bump(isolated, cfg, fake_linear):
    dispatch_action(cfg, ACTION_BUMP, "NIK-1")
    assert fake_linear["priority"] == [("NIK-1", URGENT)]


def test_dispatch_routes_skip(isolated, cfg, fake_linear):
    dispatch_action(cfg, ACTION_SKIP, "NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["backlog_state"])]


def test_dispatch_still_rejects_an_unknown_action(isolated, cfg, fake_linear):
    reply = dispatch_action(cfg, "ralph_nonsense", "NIK-1")
    assert "unknown" in reply.text.lower()
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -q -k dispatch_routes`
Expected: FAIL — the reply is "Unknown action `ralph_bump`" and no write is recorded.

- [ ] **Step 3: Route the new actions**

In `slack_listener.py`, extend the imports and `dispatch_action`:

```python
from ralph.commands import (HELP, Reply, handle_approve, handle_bump,
                            handle_discard, handle_skip, handle_slash,
                            status_text)
from ralph.slack import ACTION_APPROVE, ACTION_BUMP, ACTION_DISCARD, ACTION_SKIP
```

```python
def dispatch_action(cfg, action_id: str, ticket: str) -> Reply:
    if action_id == ACTION_APPROVE:
        return handle_approve(cfg, ticket)
    if action_id == ACTION_DISCARD:
        return handle_discard(cfg, ticket)
    if action_id == ACTION_BUMP:
        return handle_bump(cfg, ticket)
    if action_id == ACTION_SKIP:
        return handle_skip(cfg, ticket)
    return Reply(f"Unknown action `{action_id}`.")
```

- [ ] **Step 4: Send blocks when a reply has them**

In `slack_listener.py`, add a helper above `main` and use it in `on_request`, replacing the existing ternary expression:

```python
def _post_reply(web, channel: str, reply, user: str = "") -> None:
    """One place that knows how to turn a Reply into a Slack call.

    The previous inline ternary could not carry blocks, and an ephemeral reply
    needs a different method entirely.
    """
    payload = {"channel": channel, "text": reply.text}
    if reply.blocks:
        payload["blocks"] = reply.blocks
    if reply.ephemeral and user:
        web.chat_postEphemeral(user=user, **payload)
    else:
        web.chat_postMessage(**payload)
```

In `on_request`, the `slash_commands` branch becomes:

```python
            if request.type == "slash_commands":
                payload = request.payload
                reply = handle_slash(payload.get("text", ""), cfg)
                _post_reply(web, payload["channel_id"], reply,
                            payload.get("user_id", ""))
```

and the `interactive` branch's send becomes:

```python
                channel = (payload.get("channel") or {}).get("id")
                if channel:
                    _post_reply(web, channel, reply)
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_commands.py -q`
Expected: PASS — 3 new cases green.

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 214 passed.

- [ ] **Step 7: Document the commands**

In `README.md`, find the section listing the `/ralph` commands and replace it with the new `HELP` text from Task 4 Step 7, adding one line beneath it:

```markdown
`bump` sets Linear priority to Urgent; `skip` moves the ticket to Backlog,
which the gate does not treat as queued. Both are visible in Linear, and both
are reversible from Slack.
```

- [ ] **Step 8: Restart the listener and verify end to end**

Run: `powershell -Command "Restart-ScheduledTask -TaskName 'Ralph listener'"`

Then in Slack: `/ralph list`, confirm the queue renders with buttons; press **Bump** on a ticket that is not currently first; run `/ralph list` again and confirm it moved to the top; open the ticket in Linear and confirm its priority is Urgent.

Expected: the reordering is visible in both Slack and Linear. If the buttons do nothing, check `logs/listener-<date>.log` — the listener swallows handler exceptions to keep the socket alive, so failures land there rather than in Slack.

- [ ] **Step 9: Commit**

```bash
git add slack_listener.py README.md tests/test_commands.py
git commit -m "feat: wire Bump and Skip buttons through the listener"
```

---

## Deferred to a separate plan: Phase 2 (natural language)

The spec's phase 2 — DMing the bot in plain English — is deliberately **not** in this plan. It needs `im:history` and `im:read` scopes plus another app reinstall (a third `xoxb-` swap), and its whole design is to translate into the handlers built above. Those handlers should be proven in daily use before a 20B model is pointed at them.

When phase 1 has run for a while, brainstorm phase 2 against the same spec and write its own plan covering: the `message.im` subscription, `parse_intent` mirroring `ollama.parse_verdict`, the confidence floor from `local.min_confidence`, and the confirm-before-write step.

## Self-Review

**Spec coverage:** Ranking → Task 1. `set_priority` → Task 2. Backlog for skip → Tasks 4–5 (`backlog_state` config, `handle_skip`, `handle_unskip`). Handlers table → Task 4. Rendering and action ids → Tasks 3, 5. Error handling table rows for Linear unreachable / unknown ticket → Task 4 tests. Testing section → every task. NL layer, confirmation flow, Ollama parsing → explicitly deferred above.

**Known gap, accepted:** the spec's "stale confirmation" row only applies to phase 2's confirm step, which does not exist yet. Phase 1's buttons act immediately, and both actions are reversible, so there is nothing to go stale.

**Type consistency:** `rank_issues` returns `(list[dict], list[str])` in Task 1 and is consumed that way in Task 4. `URGENT` is defined once in Task 1 and imported everywhere else. `build_queue_blocks(ranked, skipped)` has the same two-argument shape in Tasks 3, 4 and its tests. `Reply.blocks` is added in Task 4 and read in Task 5. Action id constants live only in `ralph/slack.py`.
