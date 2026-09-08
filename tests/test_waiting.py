from typing import Any

import pytest

from ring.waiting import (
    FOREGROUND_OWNER,
    UNKNOWN_OWNER,
    WaitingRequest,
    event_owner,
    primary_wait,
    read_waiting_requests,
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
