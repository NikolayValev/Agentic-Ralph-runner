"""Linear access for the gate (Phase 1). Read-only.

The network call and the selection rules are deliberately separate: fetching is
a thin GraphQL wrapper, while "which ticket, if any, do we work on" is a pure
function over plain dicts so it can be tested exhaustively without a key.
"""

from __future__ import annotations

from typing import Any, Iterable

import requests

ENDPOINT = "https://api.linear.app/graphql"
TIMEOUT = 30

# Linear state *types* (not names). "unstarted" is Todo; "backlog" is Backlog.
# Requiring unstarted makes opting a ticket in a deliberate two-part gesture:
# apply the label AND move it out of Backlog into Todo. A labelled ticket left
# in Backlog is therefore parked, not queued.
ELIGIBLE_STATE_TYPES = ("unstarted",)

_QUERY = """
query EligibleIssues($teamKey: String!, $label: String!, $first: Int!) {
  issues(
    filter: {
      team: { key: { eq: $teamKey } }
      labels: { some: { name: { eq: $label } } }
    }
    first: $first
  ) {
    nodes {
      id
      identifier
      title
      priority
      createdAt
      state { name type }
      labels { nodes { name } }
    }
  }
}
"""


class LinearError(RuntimeError):
    """Linear was unreachable, rejected the key, or returned GraphQL errors."""


def _normalize(node: dict) -> dict:
    """Flatten a GraphQL node into the shape the selection rules expect."""
    return {
        "id": node["id"],
        "identifier": node["identifier"],
        "title": node.get("title") or "",
        "priority": int(node.get("priority") or 0),
        "created_at": node["createdAt"],
        "state_name": (node.get("state") or {}).get("name", ""),
        "state_type": (node.get("state") or {}).get("type", ""),
        "labels": [n["name"] for n in (node.get("labels") or {}).get("nodes", [])],
    }


def fetch_labelled_issues(
    api_key: str, team_key: str, label: str, *, first: int = 50, endpoint: str = ENDPOINT
) -> list[dict]:
    """Every issue on the team carrying `label`, in any state.

    State filtering happens in `select_ticket` so the gate can explain *why* a
    labelled ticket was skipped rather than silently not seeing it.
    """
    if not api_key:
        raise LinearError("LINEAR_API_KEY is not set")
    try:
        response = requests.post(
            endpoint,
            json={
                "query": _QUERY,
                "variables": {"teamKey": team_key, "label": label, "first": first},
            },
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise LinearError(f"Linear request failed: {exc}") from exc

    if response.status_code in (401, 403):
        raise LinearError(f"Linear rejected the API key (HTTP {response.status_code})")
    if response.status_code != 200:
        raise LinearError(f"Linear returned HTTP {response.status_code}: {response.text[:300]}")

    try:
        payload = response.json()
    except ValueError as exc:
        raise LinearError(f"Linear returned non-JSON: {response.text[:300]}") from exc

    if payload.get("errors"):
        raise LinearError(f"Linear GraphQL errors: {payload['errors']}")
    try:
        nodes = payload["data"]["issues"]["nodes"]
    except (KeyError, TypeError) as exc:
        raise LinearError(f"unexpected Linear response shape: {payload}") from exc

    return [_normalize(n) for n in nodes]


# --- pure selection rules ---------------------------------------------------

def is_eligible(issue: dict, *, eligible_label: str, repo_label: str) -> tuple[bool, str]:
    """Both labels required, and the ticket must be queued (not parked)."""
    labels = issue.get("labels") or []
    if eligible_label not in labels:
        return False, f"missing {eligible_label!r}"
    if repo_label and repo_label not in labels:
        return False, f"missing {repo_label!r}"
    if issue.get("state_type") not in ELIGIBLE_STATE_TYPES:
        return False, (
            f"state {issue.get('state_name') or '?'!r} is not queued "
            f"(need one of {ELIGIBLE_STATE_TYPES})"
        )
    return True, "eligible"


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


# --- writes (Phase 2) -------------------------------------------------------
# Deliberately performed by the wrapper via REST, not by the agent via MCP.
# Plan sec.10.3 names headless MCP OAuth as THE risk and sanctions "scoped Bash +
# REST" as the fallback; doing state transitions deterministically here also means
# a confused agent cannot leave a ticket in the wrong state.

_STATES_QUERY = """
query TeamStates($teamKey: String!) {
  workflowStates(filter: { team: { key: { eq: $teamKey } } }, first: 50) {
    nodes { id name type }
  }
}
"""

_MOVE_MUTATION = """
mutation MoveIssue($id: String!, $stateId: String!) {
  issueUpdate(id: $id, input: { stateId: $stateId }) {
    success
    issue { identifier state { name } }
  }
}
"""


def _post(api_key: str, query: str, variables: dict, endpoint: str = ENDPOINT) -> dict:
    if not api_key:
        raise LinearError("LINEAR_API_KEY is not set")
    try:
        response = requests.post(
            endpoint,
            json={"query": query, "variables": variables},
            headers={"Authorization": api_key, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
    except requests.RequestException as exc:
        raise LinearError(f"Linear request failed: {exc}") from exc
    if response.status_code in (401, 403):
        raise LinearError(f"Linear rejected the API key (HTTP {response.status_code})")
    if response.status_code != 200:
        raise LinearError(f"Linear returned HTTP {response.status_code}: {response.text[:300]}")
    payload = response.json()
    if payload.get("errors"):
        raise LinearError(f"Linear GraphQL errors: {payload['errors']}")
    return payload["data"]


def fetch_state_id(api_key: str, team_key: str, state_name: str, *, endpoint: str = ENDPOINT) -> str:
    """Resolve a workflow state name to its id, so config stays name-based."""
    data = _post(api_key, _STATES_QUERY, {"teamKey": team_key}, endpoint)
    states = data["workflowStates"]["nodes"]
    for state in states:
        if state["name"].casefold() == state_name.casefold():
            return state["id"]
    known = ", ".join(sorted(s["name"] for s in states))
    raise LinearError(f"team {team_key} has no state named {state_name!r} (has: {known})")


def move_issue(
    api_key: str, issue_key: str, state_name: str, team_key: str, *, endpoint: str = ENDPOINT
) -> str:
    """Move an issue to a named state. Returns the state it ended up in."""
    state_id = fetch_state_id(api_key, team_key, state_name, endpoint=endpoint)
    data = _post(api_key, _MOVE_MUTATION, {"id": issue_key, "stateId": state_id}, endpoint)
    result = data["issueUpdate"]
    if not result.get("success"):
        raise LinearError(f"Linear refused to move {issue_key} to {state_name!r}")
    return result["issue"]["state"]["name"]


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


_ISSUE_LABELS_QUERY = """
query IssueLabels($id: String!) {
  issue(id: $id) { id labels { nodes { id name } } }
}
"""

_SET_LABELS_MUTATION = """
mutation SetLabels($id: String!, $labelIds: [String!]) {
  issueUpdate(id: $id, input: { labelIds: $labelIds }) { success }
}
"""

_COMMENT_MUTATION = """
mutation Comment($issueId: String!, $body: String!) {
  commentCreate(input: { issueId: $issueId, body: $body }) { success }
}
"""


def add_comment(api_key: str, issue_key: str, body: str, *, endpoint: str = ENDPOINT) -> None:
    data = _post(api_key, _ISSUE_LABELS_QUERY, {"id": issue_key}, endpoint)
    issue_id = data["issue"]["id"]
    result = _post(api_key, _COMMENT_MUTATION,
                   {"issueId": issue_id, "body": body}, endpoint)
    if not result["commentCreate"]["success"]:
        raise LinearError(f"Linear refused to comment on {issue_key}")


def remove_label(api_key: str, issue_key: str, label: str, *, endpoint: str = ENDPOINT) -> bool:
    """Drop one label. Returns True if it was present and removed.

    Used to de-queue a ticket after a blocked run: without this the gate would
    re-select it every tick and spend a Claude turn reaching the same conclusion
    indefinitely. Re-queuing becomes a deliberate human act.
    """
    data = _post(api_key, _ISSUE_LABELS_QUERY, {"id": issue_key}, endpoint)
    issue = data["issue"]
    nodes = issue["labels"]["nodes"]
    if not any(n["name"] == label for n in nodes):
        return False
    remaining = [n["id"] for n in nodes if n["name"] != label]
    result = _post(api_key, _SET_LABELS_MUTATION,
                   {"id": issue["id"], "labelIds": remaining}, endpoint)
    if not result["issueUpdate"]["success"]:
        raise LinearError(f"Linear refused to update labels on {issue_key}")
    return True
