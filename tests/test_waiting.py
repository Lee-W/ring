from typing import Any

import pytest

from ring.waiting import (
    FOREGROUND_OWNER,
    UNKNOWN_OWNER,
    WaitingRequest,
    event_owner,
    primary_wait,
    read_waiting_requests,
    renew_wait,
    tool_request_id,
)


@pytest.mark.parametrize(
    ("data", "expected"),
    [
        pytest.param({}, FOREGROUND_OWNER, id="foreground"),
        pytest.param({"agent_id": "a"}, "agent:a", id="snake-case"),
        pytest.param({"agentId": "a"}, "agent:a", id="camel-case"),
        pytest.param({"agent_type": "worker"}, UNKNOWN_OWNER, id="type-only"),
        pytest.param({"agent_type": "worker", "agent_id": "a"}, "agent:a", id="id-wins"),
        pytest.param({"agent_id": "foreground"}, "agent:foreground", id="no-sentinel-collision"),
        pytest.param({"agent_id": 123, "agent_type": None}, FOREGROUND_OWNER, id="invalid-fields"),
    ],
)
def test_event_owner(data: dict[str, Any], expected: str) -> None:
    assert event_owner(data) == expected


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        pytest.param({}, {}, id="empty"),
        pytest.param({"status": "working"}, {}, id="not-waiting"),
        pytest.param(
            {"status": "waiting", "waiting_kind": "question", "waiting_detail": "answer?", "last_active": 12},
            {FOREGROUND_OWNER: WaitingRequest(FOREGROUND_OWNER, "question", "answer?", "", 12)},
            id="legacy-foreground",
        ),
        pytest.param(
            {"status": "waiting", "waiting_agent_id": "a"}, {"agent:a": WaitingRequest("agent:a")}, id="legacy-subagent"
        ),
        pytest.param({"status": "waiting", "waiting_requests": {}}, {}, id="empty-new-map-is-authoritative"),
        pytest.param(
            {"waiting_requests": {"agent:a": {"kind": "permission", "detail": "git push", "since": 20}}},
            {"agent:a": WaitingRequest("agent:a", "permission", "git push", "", 20)},
            id="new-map",
        ),
        pytest.param({"waiting_requests": {"invalid-owner": {}, "agent:a": None}}, {}, id="invalid-entries"),
        pytest.param(
            {"waiting_requests": {"agent:a": {"kind": None, "detail": [], "since": float("nan")}}},
            {"agent:a": WaitingRequest("agent:a")},
            id="invalid-optional-fields",
        ),
    ],
)
def test_read_waiting_requests(row: dict[str, Any], expected: dict[str, WaitingRequest]) -> None:
    assert read_waiting_requests(row) == expected


@pytest.mark.parametrize("owners", [("agent:a", "agent:b"), ("agent:a", FOREGROUND_OWNER)])
def test_primary_wait_prefers_foreground_then_insertion_order(owners: tuple[str, str]) -> None:
    requests = {owner: WaitingRequest(owner) for owner in owners}
    expected = FOREGROUND_OWNER if FOREGROUND_OWNER in owners else owners[0]
    assert primary_wait(requests).owner == expected


@pytest.mark.parametrize("key", ["tool_use_id", "toolUseId"])
def test_tool_request_id(key: str) -> None:
    assert tool_request_id({key: "tool-1"}) == "tool-1"
    assert tool_request_id({key: 123}) == ""


@pytest.mark.parametrize(
    ("changes", "same"),
    [
        ({"since": 200.0}, True),
        ({"detail": "richer summary", "tool_use_id": "tool-1"}, True),
        ({"tool_use_id": "tool-2"}, False),
        ({"kind": "question"}, False),
        ({"owner": "agent:b"}, False),
        ({"tool_use_id": "", "detail": "another prompt"}, False),
        ({"tool_use_id": ""}, True),
    ],
)
def test_renew_wait_only_revises_new_requests(changes: dict[str, Any], same: bool) -> None:
    from dataclasses import replace

    old = WaitingRequest("agent:a", "permission", "Bash: git push", since=100.0, tool_use_id="tool-1")
    new = renew_wait(old, replace(old, since=200.0, **{k: v for k, v in changes.items() if k != "since"}))
    assert (new.request_id == old.request_id) is same
    assert new.since == (100.0 if same else 200.0)


def test_new_wait_has_unique_id_even_with_identical_content_and_clock() -> None:
    request = WaitingRequest("foreground", "question", "continue?", since=100.0)
    first = renew_wait(None, request)
    second = renew_wait(None, request)
    assert first.request_id != second.request_id


def test_legacy_request_id_is_stable_across_reads_and_upgrade() -> None:
    row = {"status": "waiting", "last_active": 100.0, "waiting_kind": "permission"}
    old = read_waiting_requests(row)[FOREGROUND_OWNER]
    assert read_waiting_requests(row)[FOREGROUND_OWNER].request_id == old.request_id
    assert renew_wait(old, old).request_id == old.request_id


def test_revision_round_trips_and_invalid_optional_identity_is_ignored() -> None:
    from dataclasses import asdict

    request = renew_wait(None, WaitingRequest("agent:a", "question", tool_use_id="tool-1"))
    assert read_waiting_requests({"waiting_requests": {"agent:a": asdict(request)}}) == {"agent:a": request}
    invalid = read_waiting_requests({"waiting_requests": {"agent:a": {"revision": [], "tool_use_id": False}}})
    assert invalid["agent:a"].revision == invalid["agent:a"].tool_use_id == ""
