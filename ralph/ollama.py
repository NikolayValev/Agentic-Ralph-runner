"""Local model access for the Tier-1 pre-filter (Phase 6).

Why this exists: a Claude turn is the scarce resource. The gate can tell that a
ticket is *tagged*, but not whether it is worth a turn. This asks a local model
that question for free.

num_ctx is always sent explicitly. This machine's server logs
`default_num_ctx=4096` for its 16 GiB of VRAM; at that size a ticket description
plus instructions silently falls off the end of the window and the model answers
confidently about a prompt it only half received.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import requests

TIMEOUT = 180


class OllamaError(RuntimeError):
    pass


@dataclass(frozen=True)
class LocalVerdict:
    run: bool
    tier: str
    reason: str
    commit_hint: str
    confidence: float


# gpt-oss is a reasoning model. Verified on this machine:
#   /api/generate + format         -> response is EMPTY (tokens go to the
#                                     reasoning channel and never surface)
#   /api/generate + format:"json"  -> response contains chain-of-thought prose
#   /api/chat     + JSON schema    -> message.content holds clean JSON, with the
#                                     reasoning separated into message.thinking
# So: chat endpoint, schema-constrained, read message.content.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "run": {"type": "boolean"},
        "tier": {"type": "string", "enum": ["claude", "tooling"]},
        "reason": {"type": "string"},
        "commit_hint": {"type": "string"},
        "confidence": {"type": "number"},
    },
    "required": ["run", "tier", "reason", "commit_hint", "confidence"],
}

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


def chat(endpoint: str, model: str, prompt: str, *, num_ctx: int,
         schema: dict | None = None, timeout: int = TIMEOUT) -> str:
    """One non-streaming chat completion, constrained to `schema`."""
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_ctx": num_ctx, "temperature": 0},
    }
    if schema is not None:
        body["format"] = schema
    try:
        response = requests.post(
            f"{endpoint.rstrip('/')}/api/chat", json=body, timeout=timeout)
    except requests.RequestException as exc:
        raise OllamaError(f"Ollama request failed: {exc}") from exc
    if response.status_code == 404:
        raise OllamaError(
            f"Ollama has no model {model!r} (pull it first: `ollama pull {model}`)")
    if response.status_code != 200:
        raise OllamaError(f"Ollama HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    content = (payload.get("message") or {}).get("content", "")
    if not content:
        raise OllamaError(
            "Ollama returned an empty message; the model may have emitted only "
            "reasoning tokens (check the endpoint and format)")
    return content


PROMPT = """You are triaging one ticket for an autonomous coding agent.

The expensive coding agent has a strictly limited number of runs per day. Your
only job is to decide whether this ticket is worth one of them.

Answer with a single JSON object and nothing else:
{{"run": true|false, "tier": "claude"|"tooling", "reason": "<one short sentence>",
  "commit_hint": "<conventional-commit subject line>", "confidence": 0.0-1.0}}

Rules:
- "tier":"tooling" means the whole ticket is mechanical formatting, linting, or a
  patch dependency bump that a linter can fix with no model at all.
- "tier":"claude" means it needs real code reasoning.
- "run": false only if the ticket is empty, nonsensical, purely a question, or
  asks for work outside a code repository. When unsure, prefer run: true.
- Judge only the text below. Do not invent details.

TICKET {ticket}: {title}

{description}
"""


def parse_verdict(raw: str) -> LocalVerdict:
    """Parse the model's JSON strictly; a vague answer is not a decision."""
    text = raw.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise OllamaError(f"local model returned no JSON object: {text[:200]}")
    try:
        payload = json.loads(text[start:end + 1])
    except json.JSONDecodeError as exc:
        raise OllamaError(f"local model returned invalid JSON: {text[:200]}") from exc

    run = payload.get("run")
    if not isinstance(run, bool):
        raise OllamaError(f"'run' must be a boolean, got {run!r}")
    tier = payload.get("tier")
    if tier not in ("claude", "tooling"):
        raise OllamaError(f"'tier' must be claude|tooling, got {tier!r}")
    try:
        confidence = float(payload.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    return LocalVerdict(
        run=run, tier=tier,
        reason=str(payload.get("reason", ""))[:200],
        commit_hint=str(payload.get("commit_hint", ""))[:120],
        confidence=max(0.0, min(confidence, 1.0)),
    )


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
