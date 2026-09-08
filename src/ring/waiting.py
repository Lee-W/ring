"""Hook 等待來源的持久化格式；writer 與讀取／合併路徑共用。"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any
from uuid import uuid4

FOREGROUND_OWNER = "foreground"
UNKNOWN_OWNER = "unknown"


def event_owner(data: dict[str, Any]) -> str:
    for key in ("agent_id", "agentId"):
        if isinstance(value := data.get(key), str) and value:
            return f"agent:{value}"
    if any(isinstance(data.get(key), str) and data[key] for key in ("agent_type", "agentType")):
        return UNKNOWN_OWNER
    return FOREGROUND_OWNER


def tool_request_id(data: dict[str, Any]) -> str:
    return next(
        (value for key in ("tool_use_id", "toolUseId") if isinstance(value := data.get(key), str) and value), ""
    )


@dataclass(frozen=True)
class WaitingRequest:
    owner: str
    kind: str = ""
    detail: str = ""
    waiting_for: str = ""
    since: float = 0.0
    revision: str = ""
    tool_use_id: str = ""

    @property
    def request_id(self) -> str:
        """跨輪詢穩定的不透明 ID；舊 row／讀取側升紅不可每次產生隨機值。"""
        if self.revision:
            return self.revision
        identity = json.dumps([self.owner, self.since, self.tool_use_id])
        return "legacy:" + hashlib.sha256(identity.encode()).hexdigest()


def renew_wait(previous: WaitingRequest | None, current: WaitingRequest) -> WaitingRequest:
    """同一工具的重複事件／相同提醒保留識別；明確的新請求才換 ID。"""
    if previous is not None and previous.owner == current.owner and previous.kind == current.kind:
        if previous.tool_use_id and current.tool_use_id:
            same_request = previous.tool_use_id == current.tool_use_id
        else:
            same_request = (previous.detail, previous.waiting_for) == (current.detail, current.waiting_for)
        if same_request:
            return replace(
                current,
                since=previous.since,
                revision=previous.request_id,
                tool_use_id=current.tool_use_id or previous.tool_use_id,
            )
    return replace(current, revision=uuid4().hex)


def read_waiting_requests(row: dict[str, Any]) -> dict[str, WaitingRequest]:
    """舊單一 waiting row 視為一個來源；新格式的空集合不能復活舊欄位。"""
    raw = row.get("waiting_requests")
    if not isinstance(raw, dict):
        if row.get("status") != "waiting":
            return {}
        owner = f"agent:{row['waiting_agent_id']}" if row.get("waiting_agent_id") else FOREGROUND_OWNER
        raw = {
            owner: {
                "kind": row.get("waiting_kind", ""),
                "detail": row.get("waiting_detail", ""),
                "waiting_for": row.get("waiting_for", ""),
                "since": row.get("last_active", 0.0),
            }
        }
    requests = {}
    for owner, value in raw.items():
        if not isinstance(owner, str) or not isinstance(value, dict):
            continue
        if owner not in {FOREGROUND_OWNER, UNKNOWN_OWNER} and not owner.startswith("agent:"):
            continue
        since = value.get("since", 0.0)
        if not isinstance(since, (int, float)) or isinstance(since, bool) or not math.isfinite(since):
            since = 0.0
        requests[owner] = WaitingRequest(
            owner,
            **{
                key: value.get(key, "") if isinstance(value.get(key, ""), str) else ""
                for key in ("kind", "detail", "waiting_for", "revision", "tool_use_id")
            },
            since=float(since),
        )
    return requests


def primary_wait(requests: dict[str, WaitingRequest]) -> WaitingRequest:
    """優先顯示前景；其餘依加入順序，避免背景活動讓摘要一直換人。"""
    return requests.get(FOREGROUND_OWNER) or next(iter(requests.values()))
