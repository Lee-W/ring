"""Hook 等待來源的持久化格式；writer 與讀取／合併路徑共用。"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

FOREGROUND_OWNER = "foreground"
UNKNOWN_OWNER = "unknown"


def event_owner(data: dict[str, Any]) -> str:
    for key in ("agent_id", "agentId"):
        if isinstance(value := data.get(key), str) and value:
            return f"agent:{value}"
    if any(isinstance(data.get(key), str) and data[key] for key in ("agent_type", "agentType")):
        return UNKNOWN_OWNER
    return FOREGROUND_OWNER


@dataclass(frozen=True)
class WaitingRequest:
    owner: str
    kind: str = ""
    detail: str = ""
    waiting_for: str = ""
    since: float = 0.0


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
                for key in ("kind", "detail", "waiting_for")
            },
            since=float(since),
        )
    return requests


def primary_wait(requests: dict[str, WaitingRequest]) -> WaitingRequest:
    """優先顯示前景；其餘依加入順序，避免背景活動讓摘要一直換人。"""
    return requests.get(FOREGROUND_OWNER) or next(iter(requests.values()))
