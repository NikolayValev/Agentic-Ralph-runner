"""The three JSON contracts from plan v2 sec.7.

These are the seams between components. Each contract has a parse function that
validates strictly and raises ContractError with a message naming the offending
field -- a malformed contract must fail loudly at the boundary, never propagate
a half-valid dict into the pipeline.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from typing import Any

SUMMARY_MAX = 240

TASK_MODES = ("tagged",)  # "scout" is parked for MVP (sec.3)
PREFILTER_TIERS = ("claude", "tooling")
REPORT_STATUSES = ("in_review", "nothing_eligible", "blocked", "error")


class ContractError(ValueError):
    """A payload crossing a component boundary did not match its contract."""


def _require(payload: Any, field: str, typ: type) -> Any:
    if not isinstance(payload, dict):
        raise ContractError(f"expected a JSON object, got {type(payload).__name__}")
    if field not in payload:
        raise ContractError(f"missing required field {field!r}")
    value = payload[field]
    if not isinstance(value, typ):
        raise ContractError(
            f"field {field!r} must be {typ.__name__}, got {type(value).__name__}"
        )
    return value


def _one_of(field: str, value: str, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise ContractError(f"field {field!r} must be one of {allowed}, got {value!r}")
    return value


@dataclass(frozen=True)
class Task:
    """gate.py -> pipeline.  {"mode": "tagged", "ref": "NIK-123"}"""

    mode: str
    ref: str

    @staticmethod
    def parse(payload: dict) -> "Task":
        mode = _one_of("mode", _require(payload, "mode", str), TASK_MODES)
        ref = _require(payload, "ref", str).strip()
        if not ref:
            raise ContractError("field 'ref' must not be empty")
        return Task(mode=mode, ref=ref)

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


@dataclass(frozen=True)
class PreFilter:
    """local_triage.py -> dispatch.sh.  The go/no-go that saves a Claude turn."""

    run: bool
    tier: str
    reason: str
    commit_hint: str

    @staticmethod
    def parse(payload: dict) -> "PreFilter":
        run = _require(payload, "run", bool)
        tier = _one_of("tier", _require(payload, "tier", str), PREFILTER_TIERS)
        return PreFilter(
            run=run,
            tier=tier,
            reason=_require(payload, "reason", str),
            commit_hint=_require(payload, "commit_hint", str),
        )

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


@dataclass(frozen=True)
class AgentReport:
    """The agent's last stdout line -> notify.py."""

    status: str
    mode: str
    ticket: str
    branch: str
    pr_url: str
    preview_url: str
    summary: str

    @staticmethod
    def parse(payload: dict, *, require_pr_url: bool = True) -> "AgentReport":
        """Validate a report.

        Two stages, two rules. The agent's own report legitimately has an empty
        pr_url -- it has no capability to open a PR, the wrapper does that. The
        FINAL report handed to notify must carry one, or a human is being told a
        change is reviewable with nowhere to review it. Pass require_pr_url=False
        for the agent stage only.
        """
        status = _one_of("status", _require(payload, "status", str), REPORT_STATUSES)
        mode = _one_of("mode", _require(payload, "mode", str), TASK_MODES)
        summary = _require(payload, "summary", str)
        if len(summary) > SUMMARY_MAX:
            raise ContractError(
                f"field 'summary' must be <= {SUMMARY_MAX} chars, got {len(summary)}"
            )
        report = AgentReport(
            status=status,
            mode=mode,
            ticket=_require(payload, "ticket", str),
            branch=_require(payload, "branch", str),
            pr_url=_require(payload, "pr_url", str),
            preview_url=_require(payload, "preview_url", str),
            summary=summary,
        )
        # A run that claims to have produced a reviewable change must say where.
        if require_pr_url and status == "in_review" and not report.pr_url:
            raise ContractError("status 'in_review' requires a non-empty 'pr_url'")
        return report

    def to_json(self) -> str:
        return json.dumps(asdict(self), separators=(",", ":"))


def parse_report_from_output(text: str, *, require_pr_url: bool = False) -> AgentReport:
    """Extract the report from an agent's output.

    The agent is instructed to emit the report as the final JSON object. Scan
    backwards so trailing prose or a partially-written earlier object cannot
    shadow the real one.
    """
    candidates = []
    for line in text.splitlines():
        line = line.strip().removeprefix("```json").removeprefix("```").strip()
        if line.startswith("{") and line.endswith("}"):
            candidates.append(line)
    for line in reversed(candidates):
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and "status" in payload:
            return AgentReport.parse(payload, require_pr_url=require_pr_url)
    raise ContractError("no agent report JSON object found in output")
