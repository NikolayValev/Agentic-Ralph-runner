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
