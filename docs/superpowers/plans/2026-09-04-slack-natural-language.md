# Slack Natural-Language Control Implementation Plan (Phase 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a human DM the Ralph bot in plain English — "what's it doing?", "do the drizzle one next", "park that one" — and have it act, after confirming.

**Architecture:** A DM arrives as a Socket Mode `events_api` event. `interpret()` hands the text plus the current queue to the local Ollama model, constrained to a strict JSON schema, and gets back an intent. Read-only intents answer immediately; intents that write to Linear render a confirmation with buttons that route into the **existing** `handle_bump` / `handle_skip` / `handle_unskip`. The NL layer holds no privileges of its own — it can only propose one of the actions the buttons already perform.

**Tech Stack:** Python 3.10+, `slack_sdk` Socket Mode, Ollama (`gpt-oss:20b`) via `ralph/ollama.py`, pytest with `monkeypatch`.

**Spec:** `docs/superpowers/specs/2026-09-02-slack-queue-prioritization-design.md` (Phase 2 section)

## Prerequisite — human, before Task 4 can be verified

The Slack app needs, under *OAuth & Permissions → Bot Token Scopes*:

- `im:history` — read DM content
- `im:read` — see DM conversations

Plus *App Home → Message Tab → enable*, and *Event Subscriptions → Subscribe to bot events → `message.im`*.

Then **reinstall the app**, which issues a new `xoxb-` token that must replace `SLACK_BOT_TOKEN` in `.env`, and restart the listener task.

Verify with:
```bash
curl -s -D - -o /dev/null -X POST https://slack.com/api/auth.test \
  -H "Authorization: Bearer $SLACK_BOT_TOKEN" | grep -i x-oauth-scopes
```
`im:history` and `im:read` must appear. Tasks 1–3 need none of this and can be built first.

## Global Constraints

- **ANTHROPIC_API_KEY must never be set.** The NL layer calls Ollama only. Conversation must never consume the Claude subscription the loop depends on — that is the entire reason the local tier exists.
- **No live network in the test suite.** Monkeypatch `ollama.chat` and the Linear functions. No HTTP mocking library.
- **Every Linear write is confirmed before it happens.** The model proposes; the human commits.
- **Confidence floor is `local.min_confidence` from `config.yaml`** (currently `0.6`). Below it, ask — never guess.
- **The listener must never reply to itself.** A bot message that triggers a bot reply is an infinite loop that will rate-limit the workspace.
- Run the suite with `python -m pytest -q` from `C:\Users\Nikolay\code\ralph`. Baseline is **258 passing**.
- Commit after every task.

## Deviation from the spec, deliberate

The spec says "every write is confirmed with buttons". This plan confirms **Linear writes** (`bump`, `skip`, `unskip`) but executes `stop` and `go` immediately. `stop` is the kill switch: putting a confirmation step in front of it makes the emergency brake slower at exactly the moment it matters, and both it and `go` are one-word reversible. `status` and `list` are read-only and also immediate.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `ralph/ollama.py` | Local model access, strict parsing | Modify — add `INTENT_SCHEMA`, `INTENT_PROMPT`, `Intent`, `parse_intent` |
| `ralph/conversation.py` | Turn a sentence + the queue into an intent or a question | **Create** |
| `ralph/slack.py` | Pure Block Kit construction | Modify — add `ACTION_CONFIRM`/`ACTION_CANCEL`, `build_confirm_blocks` |
| `ralph/commands.py` | Transport-free command behaviour | Modify — add `handle_confirm`, `handle_cancel` |
| `slack_listener.py` | Socket Mode transport | Modify — route `message.im` events and the two new actions |
| `config.yaml` | Policy | Modify — add `conversation.enabled` |
| `tests/test_intent.py` | Intent parsing | **Create** |
| `tests/test_conversation.py` | Interpretation, floors, failure modes | **Create** |
| `tests/test_commands.py` | Confirm/cancel handlers | Modify — append |
| `tests/test_scripts.py` | — | unchanged |

---

### Task 1: Parse an intent from the local model

**Files:**
- Modify: `ralph/ollama.py`
- Test: `tests/test_intent.py` (create)

**Interfaces:**
- Consumes: existing `chat(endpoint, model, prompt, *, num_ctx, schema, timeout) -> str`, `OllamaError`
- Produces:
  - `ACTIONS = ("bump", "skip", "unskip", "list", "status", "stop", "go", "unknown")`
  - `@dataclass(frozen=True) Intent(action: str, ticket: str, confidence: float)`
  - `INTENT_SCHEMA: dict`, `INTENT_PROMPT: str`
  - `parse_intent(raw: str) -> Intent`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_intent.py`:

```python
"""Strict parsing of the local model's intent JSON.

Mirrors parse_verdict: a vague answer is not a decision. Everything here is
pure string -> Intent; no network, no Slack.
"""

from __future__ import annotations

import pytest

from ralph.ollama import Intent, OllamaError, parse_intent


def test_parses_a_clean_intent():
    got = parse_intent('{"action":"bump","ticket":"NIK-105","confidence":0.9}')
    assert got == Intent(action="bump", ticket="NIK-105", confidence=0.9)


def test_extracts_json_from_surrounding_prose():
    """gpt-oss is a reasoning model and sometimes narrates around its answer."""
    raw = 'Sure! Here is the result:\n{"action":"skip","ticket":"NIK-1","confidence":0.7}\nHope that helps.'
    assert parse_intent(raw).action == "skip"


def test_an_unknown_action_becomes_unknown_not_an_error():
    """A model inventing an action must degrade to a clarifying question, not
    a crash that takes the listener down."""
    got = parse_intent('{"action":"deploy","ticket":"NIK-1","confidence":0.9}')
    assert got.action == "unknown"


def test_a_missing_confidence_is_zero_not_one():
    """Defaulting high would let a silent model authorise a write."""
    assert parse_intent('{"action":"bump","ticket":"NIK-1"}').confidence == 0.0


def test_confidence_is_clamped():
    assert parse_intent('{"action":"bump","ticket":"NIK-1","confidence":7}').confidence == 1.0
    assert parse_intent('{"action":"bump","ticket":"NIK-1","confidence":-3}').confidence == 0.0


def test_the_ticket_is_uppercased_and_trimmed():
    assert parse_intent('{"action":"bump","ticket":" nik-105 ","confidence":0.9}').ticket == "NIK-105"


def test_no_json_at_all_raises():
    with pytest.raises(OllamaError, match="no JSON"):
        parse_intent("I'm not sure what you mean")


def test_malformed_json_raises():
    with pytest.raises(OllamaError, match="invalid JSON"):
        parse_intent('{"action":"bump", ticket:}')
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_intent.py -q`
Expected: FAIL — `ImportError: cannot import name 'Intent' from 'ralph.ollama'`

- [ ] **Step 3: Add the schema, prompt and dataclass**

In `ralph/ollama.py`, after `VERDICT_SCHEMA`:

```python
# The actions a conversation may propose. Deliberately the same verbs the
# slash commands and buttons already perform: the NL layer is a translator,
# never a new capability.
ACTIONS = ("bump", "skip", "unskip", "list", "status", "stop", "go", "unknown")


@dataclass(frozen=True)
class Intent:
    action: str
    ticket: str
    confidence: float


INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": list(ACTIONS)},
        "ticket": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["action", "ticket", "confidence"],
}


INTENT_PROMPT = """You translate one message into a single command for a coding bot.

Answer with a single JSON object and nothing else:
{{"action": "bump"|"skip"|"unskip"|"list"|"status"|"stop"|"go"|"unknown",
  "ticket": "<ticket id, or empty string>", "confidence": 0.0-1.0}}

Meanings:
- "bump": work this ticket next.
- "skip": stop considering this ticket for now.
- "unskip": put a previously skipped ticket back in the queue.
- "list": show the queue. "status": show whether the bot is running.
- "stop": pause the bot. "go": resume it.
- "unknown": anything else, including questions you cannot answer with one
  of the actions above.

Rules:
- "ticket" must be one of the ids in the queue below, copied exactly, or "".
- The user may describe a ticket instead of naming it ("the drizzle one").
  Match it against the titles below. If nothing matches clearly, use "unknown".
- Set confidence below 0.6 whenever you are guessing. Guessing wrongly makes
  the bot do the wrong work; asking is free.
- Judge only the message and the queue below. Do not invent ticket ids.

The queue right now:
{queue}

The message: {message}
"""
```

- [ ] **Step 4: Add the parser**

At the end of `ralph/ollama.py`:

```python
def parse_intent(raw: str) -> Intent:
    """Parse the model's JSON strictly. An unrecognised action is not an error.

    A model that invents an action must degrade into a clarifying question,
    not an exception: this runs inside the listener's request handler, and a
    crash there is a dropped message the human never learns about.
    """
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise OllamaError(f"local model returned no JSON object: {text[:200]}")
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise OllamaError(f"local model returned invalid JSON: {text[:200]}") from exc

    action = payload.get("action")
    if action not in ACTIONS:
        action = "unknown"
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return Intent(
        action=action,
        ticket=str(payload.get("ticket", "")).strip().upper(),
        confidence=max(0.0, min(confidence, 1.0)),
    )
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_intent.py -q`
Expected: PASS, 8 passed

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 266 passed (258 + 8)

- [ ] **Step 7: Commit**

```bash
git add ralph/ollama.py tests/test_intent.py
git commit -m "feat: parse a natural-language intent from the local model"
```

---

### Task 2: Interpret a message against the live queue

**Files:**
- Create: `ralph/conversation.py`
- Test: `tests/test_conversation.py` (create)

**Interfaces:**
- Consumes: `Intent`, `INTENT_SCHEMA`, `INTENT_PROMPT`, `parse_intent`, `chat`, `OllamaError` from `ralph.ollama`; `rank_issues`, `fetch_labelled_issues`, `LinearError` from `ralph.linear`
- Produces:
  - `format_queue(ranked: list[dict]) -> str`
  - `interpret(cfg, message: str) -> tuple[Intent | None, str]` — returns `(intent, "")` on success, or `(None, question)` when it cannot act

- [ ] **Step 1: Write the failing tests**

Create `tests/test_conversation.py`:

```python
"""Turning a sentence into an intent, and refusing to guess.

The model never reaches Linear here: interpret() only decides WHAT to
propose. Executing it is the confirmation step's job.
"""

from __future__ import annotations

import pytest

import ralph.conversation as conversation
from ralph.config import load
from ralph.ollama import Intent, OllamaError


@pytest.fixture
def cfg():
    return load(check_env=False)


@pytest.fixture
def queue(monkeypatch):
    """Two tickets, so 'the drizzle one' has something to match against."""
    issues = [
        {"identifier": "NIK-111", "title": "Add haptics to breathing timer",
         "priority": 0, "created_at": "2026-01-01T00:00:00Z", "state_type": "unstarted",
         "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"], "id": "u1"},
        {"identifier": "NIK-115", "title": "Migrate to Drizzle 0.31",
         "priority": 0, "created_at": "2026-01-02T00:00:00Z", "state_type": "unstarted",
         "labels": ["autonomous-eligible", "repo:quitting-smoking-tracker"], "id": "u2"},
    ]
    monkeypatch.setattr(conversation, "fetch_labelled_issues", lambda *a, **k: issues)
    return issues


def _model(monkeypatch, reply: str):
    monkeypatch.setattr(conversation, "chat", lambda *a, **k: reply)


def test_the_queue_is_given_to_the_model(cfg, queue, monkeypatch):
    """Without the queue in the prompt, 'the drizzle one' cannot resolve and
    the model is free to invent an id."""
    seen = {}
    monkeypatch.setattr(conversation, "chat",
                        lambda endpoint, model, prompt, **k: seen.setdefault("prompt", prompt)
                        or '{"action":"bump","ticket":"NIK-115","confidence":0.9}')
    conversation.interpret(cfg, "do the drizzle one next")
    assert "NIK-115" in seen["prompt"] and "Migrate to Drizzle" in seen["prompt"]


def test_a_confident_intent_is_returned(cfg, queue, monkeypatch):
    _model(monkeypatch, '{"action":"bump","ticket":"NIK-115","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "do the drizzle one next")
    assert intent == Intent("bump", "NIK-115", 0.9) and question == ""


def test_low_confidence_asks_instead_of_acting(cfg, queue, monkeypatch):
    """Below local.min_confidence the bot must ask. Acting on a guess is how
    the wrong ticket gets parked overnight."""
    _model(monkeypatch, '{"action":"skip","ticket":"NIK-111","confidence":0.2}')
    intent, question = conversation.interpret(cfg, "maybe park something")
    assert intent is None and "not sure" in question.lower()


def test_an_unknown_action_asks(cfg, queue, monkeypatch):
    _model(monkeypatch, '{"action":"unknown","ticket":"","confidence":0.9}')
    intent, question = conversation.interpret(cfg, "what is the airspeed of a swallow")
    assert intent is None and question


def test_a_ticket_outside_the_queue_is_refused(cfg, queue, monkeypatch):
    """The model can hallucinate an id that parses perfectly."""
    _model(monkeypatch, '{"action":"bump","ticket":"NIK-999","confidence":0.95}')
    intent, question = conversation.interpret(cfg, "bump 999")
    assert intent is None and "NIK-999" in question


def test_ollama_being_down_degrades_to_a_hint(cfg, queue, monkeypatch):
    """A local model outage must not look like the bot ignoring you."""
    def boom(*a, **k):
        raise OllamaError("connection refused")
    monkeypatch.setattr(conversation, "chat", boom)
    intent, question = conversation.interpret(cfg, "bump the drizzle one")
    assert intent is None and "/ralph" in question


def test_linear_being_down_degrades_to_a_hint(cfg, monkeypatch):
    from ralph.linear import LinearError

    def boom(*a, **k):
        raise LinearError("Linear request failed")
    monkeypatch.setattr(conversation, "fetch_labelled_issues", boom)
    intent, question = conversation.interpret(cfg, "bump the drizzle one")
    assert intent is None and "queue" in question.lower()


def test_read_only_actions_need_no_ticket(cfg, queue, monkeypatch):
    _model(monkeypatch, '{"action":"status","ticket":"","confidence":0.95}')
    intent, question = conversation.interpret(cfg, "what are you doing")
    assert intent is not None and intent.action == "status"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_conversation.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'ralph.conversation'`

- [ ] **Step 3: Create the module**

Create `ralph/conversation.py`:

```python
"""Phase 2: turn a sentence into one of the actions the bot already performs.

This module decides WHAT to propose. It never writes to Linear -- executing a
proposal is the confirmation step's job, which routes into the same handlers
the buttons use. The NL layer therefore has no privileges of its own: the
worst a misparse can do is offer the wrong button.
"""

from __future__ import annotations

from ralph.config import Config
from ralph.linear import LinearError, fetch_labelled_issues, rank_issues
from ralph.ollama import (INTENT_PROMPT, INTENT_SCHEMA, Intent, OllamaError,
                          chat, parse_intent)
import os

# Actions that read nothing from the queue and so need no ticket id.
TICKETLESS = ("list", "status", "stop", "go")

FALLBACK = "Try `/ralph help` for the commands I understand."


def format_queue(ranked: list[dict]) -> str:
    """The queue as the model sees it. Ids and titles only -- it matches
    'the drizzle one' against titles, and copies the id verbatim."""
    if not ranked:
        return "(the queue is empty)"
    return "\n".join(
        f"- {i['identifier']}: {(i.get('title') or '')[:80]}" for i in ranked)


def interpret(cfg: Config, message: str) -> tuple[Intent | None, str]:
    """Return (intent, "") when confident, or (None, question) when not."""
    try:
        issues = fetch_labelled_issues(
            os.environ.get("LINEAR_API_KEY", ""),
            cfg.linear["team_key"], cfg.linear["eligible_label"])
        ranked, _ = rank_issues(
            issues,
            eligible_label=cfg.linear["eligible_label"],
            repo_label=cfg.linear.get("repo_label", ""))
    except (LinearError, ValueError, KeyError) as exc:
        return None, f":warning: I could not read the queue: {exc}"

    local = cfg.local
    prompt = INTENT_PROMPT.format(
        queue=format_queue(ranked), message=message.strip()[:500])
    try:
        raw = chat(local["endpoint"], local["model"], prompt,
                   num_ctx=int(local["num_ctx"]), schema=INTENT_SCHEMA)
        intent = parse_intent(raw)
    except OllamaError as exc:
        return None, f":warning: my local model is not answering ({exc}). {FALLBACK}"

    floor = float(local.get("min_confidence", 0.6))
    if intent.action == "unknown":
        return None, f"I did not follow that. {FALLBACK}"
    if intent.confidence < floor:
        return None, (
            f"I am not sure enough to act on that (confidence "
            f"{intent.confidence:.0%}). {FALLBACK}")

    if intent.action not in TICKETLESS:
        queued = {i["identifier"] for i in ranked}
        if intent.ticket not in queued:
            listed = ", ".join(sorted(queued)) or "nothing"
            return None, (
                f"{intent.ticket or 'That ticket'} is not in the queue. "
                f"Currently queued: {listed}.")

    return intent, ""
```

- [ ] **Step 4: Run the tests**

Run: `python -m pytest tests/test_conversation.py -q`
Expected: PASS, 8 passed

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 274 passed

- [ ] **Step 6: Commit**

```bash
git add ralph/conversation.py tests/test_conversation.py
git commit -m "feat: interpret a DM against the live queue"
```

---

### Task 3: Confirm before writing

**Files:**
- Modify: `ralph/slack.py`
- Modify: `ralph/commands.py`
- Test: `tests/test_commands.py` (append)

**Interfaces:**
- Consumes: `handle_bump`, `handle_skip`, `handle_unskip`, `handle_list`, `status_text`, `Reply` from `ralph.commands`; `Intent` from `ralph.ollama`
- Produces:
  - `ACTION_CONFIRM = "ralph_confirm"`, `ACTION_CANCEL = "ralph_cancel"` in `ralph/slack.py`
  - `build_confirm_blocks(action: str, ticket: str, sentence: str) -> list[dict]` in `ralph/slack.py`
  - `handle_confirm(cfg, value: str) -> Reply` and `handle_cancel(cfg, value: str) -> Reply` in `ralph/commands.py`, where `value` is `"<action>::<ticket>"`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_commands.py`:

```python
# --- natural-language confirmation ------------------------------------------

from ralph.commands import handle_cancel, handle_confirm
from ralph.slack import ACTION_CANCEL, ACTION_CONFIRM, build_confirm_blocks


def test_confirm_blocks_carry_the_action_and_ticket():
    """The button value is the only state that survives the round trip."""
    blocks = build_confirm_blocks("bump", "NIK-115", "do the drizzle one next")
    actions = [b for b in blocks if b["type"] == "actions"][0]
    values = {e["action_id"]: e["value"] for e in actions["elements"]}
    assert values[ACTION_CONFIRM] == "bump::NIK-115"
    assert values[ACTION_CANCEL] == "bump::NIK-115"


def test_confirm_blocks_quote_what_was_understood():
    """The human is approving an interpretation, so it must be visible."""
    blocks = build_confirm_blocks("skip", "NIK-111", "park the haptics one")
    rendered = json.dumps(blocks)
    assert "NIK-111" in rendered and "skip" in rendered.lower()


def test_confirming_a_bump_performs_the_bump(isolated, cfg, fake_linear):
    reply = handle_confirm(cfg, "bump::NIK-1")
    assert fake_linear["priority"] == [("NIK-1", URGENT)]
    assert reply.ephemeral is False


def test_confirming_a_skip_performs_the_skip(isolated, cfg, fake_linear):
    handle_confirm(cfg, "skip::NIK-1")
    assert fake_linear["moves"] == [("NIK-1", cfg.linear["backlog_state"])]


def test_cancelling_performs_nothing(isolated, cfg, fake_linear):
    reply = handle_cancel(cfg, "bump::NIK-1")
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []
    assert "cancel" in reply.text.lower()


def test_a_malformed_confirm_value_writes_nothing(isolated, cfg, fake_linear):
    """A stale or hand-crafted button payload must not reach Linear."""
    reply = handle_confirm(cfg, "garbage")
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []
    assert "could not" in reply.text.lower()


def test_an_unknown_action_in_a_confirm_writes_nothing(isolated, cfg, fake_linear):
    reply = handle_confirm(cfg, "deploy::NIK-1")
    assert fake_linear["priority"] == [] and fake_linear["moves"] == []
    assert "could not" in reply.text.lower()
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -q`
Expected: FAIL — `ImportError: cannot import name 'handle_confirm' from 'ralph.commands'`

- [ ] **Step 3: Add the confirmation blocks**

In `ralph/slack.py`, beside the other action ids:

```python
ACTION_CONFIRM = "ralph_confirm"
ACTION_CANCEL = "ralph_cancel"
```

And at the end of the file:

```python
def build_confirm_blocks(action: str, ticket: str, sentence: str) -> list[dict]:
    """Ask before acting on an interpretation of what someone said.

    The quoted sentence matters: the human is approving a reading of their
    words, not just an action, and a misparse is only obvious when both are
    shown together. The button value carries the whole decision, because it
    is the only state that survives the round trip to Slack and back.
    """
    value = f"{action}::{ticket}"
    return [
        {"type": "section",
         "text": {"type": "mrkdwn",
                  "text": f"You said: _{escape_mrkdwn(sentence[:200])}_\n"
                          f"I read that as *{action}* "
                          f"<{linear_url(ticket)}|{ticket}>."}},
        {"type": "actions",
         "block_id": f"ralph_confirm::{ticket}",
         "elements": [
             {"type": "button", "action_id": ACTION_CONFIRM,
              "text": {"type": "plain_text", "text": "Do it"},
              "style": "primary", "value": value},
             {"type": "button", "action_id": ACTION_CANCEL,
              "text": {"type": "plain_text", "text": "Cancel"},
              "value": value},
         ]},
    ]
```

- [ ] **Step 4: Add the handlers**

In `ralph/commands.py`, after `handle_unskip`:

```python
# The confirmation button's value is "<action>::<ticket>". Only these actions
# may be confirmed: a value naming anything else is a stale or hand-crafted
# payload and must reach nothing.
CONFIRMABLE = {
    "bump": handle_bump,
    "skip": handle_skip,
    "unskip": handle_unskip,
}


def _split_confirm(value: str) -> tuple[str, str]:
    action, _, ticket = (value or "").partition("::")
    return action.strip().lower(), ticket.strip().upper()


def handle_confirm(cfg: Config, value: str) -> Reply:
    """Perform a previously proposed action, after the human approved it."""
    action, ticket = _split_confirm(value)
    handler = CONFIRMABLE.get(action)
    if handler is None or not ticket:
        return Reply(f":warning: could not act on `{value}`; ask me again.",
                     ephemeral=False)
    return handler(cfg, ticket)


def handle_cancel(cfg: Config, value: str) -> Reply:
    """Drop a proposal. Nothing was written, so there is nothing to undo."""
    action, ticket = _split_confirm(value)
    return Reply(f":x: Cancelled {action or 'that'} {ticket}; nothing changed.",
                 ephemeral=False)
```

- [ ] **Step 5: Run the tests**

Run: `python -m pytest tests/test_commands.py -q`
Expected: PASS — 7 new cases green

- [ ] **Step 6: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 281 passed

- [ ] **Step 7: Commit**

```bash
git add ralph/slack.py ralph/commands.py tests/test_commands.py
git commit -m "feat: confirm an interpreted action before writing to Linear"
```

---

### Task 4: Route DMs through the listener

**Files:**
- Modify: `slack_listener.py`
- Modify: `config.yaml`
- Test: `tests/test_commands.py` (append)

**Interfaces:**
- Consumes: `interpret` from `ralph.conversation`; `handle_confirm`, `handle_cancel` from `ralph.commands`; `ACTION_CONFIRM`, `ACTION_CANCEL`, `build_confirm_blocks` from `ralph.slack`
- Produces: `is_human_dm(event: dict) -> bool` and `handle_dm(cfg, text: str) -> Reply` in `slack_listener.py`; `dispatch_action` routes the two new ids

- [ ] **Step 1: Add the config switch**

In `config.yaml`, after the `local:` block:

```yaml
conversation:
  # Phase 2. Off disables DM handling entirely; slash commands are unaffected.
  enabled: true
```

In `ralph/config.py`, add `"conversation"` to `REQUIRED_SECTIONS` and add the accessor beside the others:

```python
    @property
    def conversation(self) -> dict: return self.raw["conversation"]
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_commands.py`:

```python
# --- DM routing --------------------------------------------------------------

from slack_listener import handle_dm, is_human_dm


def test_a_bot_message_is_never_treated_as_a_dm():
    """The bot's own messages arrive as events too. Replying to them is an
    infinite loop that will rate-limit the workspace."""
    assert is_human_dm({"type": "message", "channel_type": "im",
                        "bot_id": "B123", "text": "hi"}) is False


def test_a_message_edit_is_ignored():
    """message_changed re-delivers old text and would re-run the action."""
    assert is_human_dm({"type": "message", "channel_type": "im",
                        "subtype": "message_changed", "text": "hi"}) is False


def test_a_channel_message_is_not_a_dm():
    assert is_human_dm({"type": "message", "channel_type": "channel",
                        "text": "hi"}) is False


def test_a_plain_human_dm_is_accepted():
    assert is_human_dm({"type": "message", "channel_type": "im",
                        "user": "U1", "text": "status"}) is True


def test_a_dm_proposing_a_write_asks_for_confirmation(isolated, cfg, monkeypatch):
    import slack_listener
    from ralph.ollama import Intent
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (Intent("bump", "NIK-1", 0.9), ""))
    reply = handle_dm(cfg, "do the first one next")
    assert reply.blocks, "a write must be confirmed, not performed"


def test_a_dm_asking_for_status_answers_immediately(isolated, cfg, monkeypatch):
    import slack_listener
    from ralph.ollama import Intent
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (Intent("status", "", 0.95), ""))
    reply = handle_dm(cfg, "what are you doing")
    assert "Ralph" in reply.text and not reply.blocks


def test_a_dm_the_model_could_not_read_returns_the_question(isolated, cfg, monkeypatch):
    import slack_listener
    monkeypatch.setattr(slack_listener, "interpret",
                        lambda c, m: (None, "I did not follow that."))
    reply = handle_dm(cfg, "asdf")
    assert "did not follow" in reply.text
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `python -m pytest tests/test_commands.py -q -k "dm or bot_message"`
Expected: FAIL — `ImportError: cannot import name 'handle_dm' from 'slack_listener'`

- [ ] **Step 4: Add DM handling**

In `slack_listener.py`, extend the imports:

```python
from ralph.commands import (HELP, Reply, handle_approve, handle_bump,
                            handle_cancel, handle_confirm, handle_discard,
                            handle_list, handle_skip, handle_slash,
                            status_text)
from ralph.conversation import interpret
from ralph.slack import (ACTION_APPROVE, ACTION_BUMP, ACTION_CANCEL,
                         ACTION_CONFIRM, ACTION_DISCARD, ACTION_SKIP,
                         build_confirm_blocks)
```

Add above `main`:

```python
# Actions that write to Linear, and so must be confirmed before they happen.
NEEDS_CONFIRMATION = ("bump", "skip", "unskip")


def is_human_dm(event: dict) -> bool:
    """A real person typing in a DM -- and nothing else.

    The bot's own posts arrive as events too, and replying to them is an
    infinite loop. `message_changed` re-delivers old text, which would run the
    same action twice.
    """
    if event.get("type") != "message":
        return False
    if event.get("channel_type") != "im":
        return False
    if event.get("bot_id") or event.get("subtype"):
        return False
    return bool(event.get("user") and event.get("text"))


def handle_dm(cfg, text: str) -> Reply:
    """Interpret a sentence; propose writes, perform reads."""
    intent, question = interpret(cfg, text)
    if intent is None:
        return Reply(question, ephemeral=False)

    if intent.action in NEEDS_CONFIRMATION:
        return Reply(
            f"Confirm {intent.action} {intent.ticket}?",
            ephemeral=False,
            blocks=build_confirm_blocks(intent.action, intent.ticket, text))

    if intent.action == "list":
        return handle_list(cfg)
    if intent.action == "status":
        return Reply(status_text(cfg), ephemeral=False)
    # stop and go are the kill switch: deliberately immediate, and both are
    # one word to reverse. A confirmation step here would slow the emergency
    # brake at exactly the moment it matters.
    return handle_slash(intent.action, cfg)
```

- [ ] **Step 5: Route the events and the buttons**

In `slack_listener.py`, extend `dispatch_action`:

```python
    if action_id == ACTION_CONFIRM:
        return handle_confirm(cfg, ticket)
    if action_id == ACTION_CANCEL:
        return handle_cancel(cfg, ticket)
```

Note `ticket_from_payload` already returns the button's `value`, which for these two carries `"<action>::<ticket>"` — exactly what the handlers expect.

In `on_request`, add a branch after the `interactive` one:

```python
            elif request.type == "events_api":
                event = (request.payload.get("event") or {})
                if not cfg.conversation.get("enabled", True):
                    return
                if not is_human_dm(event):
                    return
                reply = handle_dm(cfg, event.get("text", ""))
                _post_reply(web, event["channel"], reply)
```

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests/test_commands.py -q`
Expected: PASS — 7 new cases green

- [ ] **Step 7: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 288 passed

- [ ] **Step 8: Verify against real Slack**

Requires the prerequisite at the top of this plan. Restart the listener:

```powershell
Restart-ScheduledTask -TaskName "Ralph listener"
```

Then DM the bot:
1. `what are you doing` → the status block, immediately.
2. `show me the queue` → the queue with Bump/Skip buttons.
3. `do the <something> one next` → a confirmation quoting your sentence. Press **Do it**; confirm the priority changed in Linear.
4. `asdfgh` → a clarifying reply, and no write.

Expected: no message ever triggers a second bot message. If the bot replies to itself, stop the listener immediately (`Stop-ScheduledTask -TaskName "Ralph listener"`) — `is_human_dm` is the guard and something reached past it.

- [ ] **Step 9: Commit**

```bash
git add slack_listener.py config.yaml ralph/config.py tests/test_commands.py
git commit -m "feat: answer direct messages in plain English"
```

---

### Task 5: Document it

**Files:**
- Modify: `README.md`
- Modify: `TODO.md`

- [ ] **Step 1: Update the README**

In `README.md`, under the `### Slack commands` subsection, append:

```markdown
### Talking to it

DM the bot in plain English. It matches what you say against the current
queue using the local model, and asks before anything that writes:

    you    do the drizzle one next
    ralph  You said: "do the drizzle one next"
           I read that as bump NIK-115.  [Do it] [Cancel]

Reads (`status`, `list`) happen immediately. `stop` and `go` are also
immediate: the kill switch should not need a second click.

Interpretation runs entirely on the local model (`local.model` in
`config.yaml`) and never consumes the Claude subscription. If it is unsure -
below `local.min_confidence` - it asks rather than guesses, and a ticket it
names that is not in the queue is refused outright.

Turn it off with `conversation.enabled: false`; slash commands are unaffected.
```

- [ ] **Step 2: Update TODO.md**

Replace the "Phase 2 of the queue work" follow-up bullet with:

```markdown
- Phase 2 (natural language over Slack DM) is implemented. It needs the
  `im:history` and `im:read` scopes and a Message Tab enabled; if DMs are not
  answered, check `logs/listener-<date>.log` first.
```

- [ ] **Step 3: Run the whole suite**

Run: `python -m pytest -q`
Expected: PASS — 288 passed

- [ ] **Step 4: Commit**

```bash
git add README.md TODO.md
git commit -m "docs: describe talking to the bot in plain English"
```

## Self-Review

**Spec coverage:** Surface (`message.im`, scopes, reinstall) → prerequisite + Task 4. `parse_intent` mirroring `parse_verdict` → Task 1. Confidence floor from `local.min_confidence` → Task 2. Queue given to the model so "the drizzle one" resolves → Task 2. Confirm-before-write → Task 3. Ollama only, never Claude → Global Constraints, enforced by Task 2 calling only `ralph.ollama`. Error-handling rows (Ollama down, unknown ticket, low confidence) → Task 2's tests.

**Deviation, stated above:** the spec's "every write is confirmed" is implemented as "every *Linear* write is confirmed"; `stop`/`go` are immediate, with the reasoning recorded.

**One risk I could not design away:** `is_human_dm` is the only thing standing between this and a self-replying bot. It is tested four ways (bot_id, subtype, channel_type, happy path), and Task 4's manual step tells the operator to kill the listener if a loop ever appears.

**Type consistency:** `Intent(action, ticket, confidence)` is defined in Task 1 and consumed unchanged in Tasks 2 and 4. `interpret(cfg, message) -> tuple[Intent | None, str]` is produced in Task 2 and consumed in Task 4. The confirm value format `"<action>::<ticket>"` is written by `build_confirm_blocks` in Task 3 and parsed by `_split_confirm` in the same task, then carried unchanged through `ticket_from_payload` in Task 4.
