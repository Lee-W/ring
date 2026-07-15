"""原始 hook payload 取證 logger——診斷用，預設關閉。

診斷「claude-code 裸 PermissionRequest 被誤判 WAITING」與「codex 裸 PermissionRequest
永遠判 WORKING」這類問題時，需要看到 hook 實際收到的原始 event payload——現有的
registry（``~/.config/ring/sessions/*.json``）與 ``events.jsonl`` 都只留正規化後的
status，原始資料在 ``hook_protocol.normalize`` 跑完就遺失了。

這支 logger 在 hook 收到 stdin 的當下、任何狀態判定/改寫之前，把原始 payload 整段
append 一行，供事後分析用。設計原則跟 ``stats.log_transition`` 一致：

- append-only、JSONL，一行一事件
- 檔案超過上限自動砍半保新，不無限成長
- 任何錯誤（含讀 config 失敗、payload 無法序列化）一律安靜吞掉，絕不影響 hook 主流程

開關：``debug_payload_log`` config 鍵，或環境變數 ``RING_DEBUG_PAYLOAD_LOG``
（``1``/``true``/``yes``/``on`` 開，``0``/``false``/``no``/``off`` 關；env 優先於 config）。
**預設關閉**——payload 可能含使用者輸入/檔案內容，開了才寫。
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

PAYLOAD_LOG_PATH = Path.home() / ".config" / "ring" / "hook_payloads.jsonl"

# log 檔上限；超過就砍半保新（append 前檢查）。原始 payload 比 events.jsonl 的轉換記錄
# 大很多（可能含完整 tool_input／transcript 片段），給足空間再砍半。
_MAX_BYTES = 20 * 1024 * 1024

_ENV_FLAG = "RING_DEBUG_PAYLOAD_LOG"

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def payload_log_enabled() -> bool:
    """是否要記原始 payload：env var 優先，其次 config 鍵，預設關閉。"""
    env = os.environ.get(_ENV_FLAG, "").strip().lower()
    if env in _TRUE:
        return True
    if env in _FALSE:
        return False
    try:
        from ring.config import get_config

        return get_config().debug_payload_log
    except Exception:
        return False


def _raw_event_name(data: Mapping[str, Any]) -> str:
    """從原始 payload 猜事件名（只為了記錄好讀，不影響任何判定邏輯）。"""
    for key in ("event", "event_name", "hook_event_name", "hookEventName"):
        v = data.get(key)
        if isinstance(v, str) and v:
            return v
    return ""


def maybe_log_raw_payload(
    provider: str,
    data: Mapping[str, Any],
    *,
    path: Path | None = None,
    now: float | None = None,
) -> None:
    """開關開啟時，把 hook 收到的原始 payload 整段 append 一行（診斷用）。

    呼叫端應放在 hook 處理最前端，早於 ``hook_protocol`` 的任何狀態判定/改寫。
    開關預設關閉（見模組 docstring）；任何錯誤一律安靜吞掉，呼叫端不需要自己包 try。
    """
    try:
        if not payload_log_enabled():
            return
        p = path or PAYLOAD_LOG_PATH
        line = json.dumps(
            {
                "ts": now if now is not None else time.time(),
                "provider": provider,
                "event": _raw_event_name(data),
                "payload": data,
            },
            ensure_ascii=False,
            default=str,
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        _trim_if_oversized(p)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _trim_if_oversized(p: Path) -> None:
    """log 超過 ``_MAX_BYTES`` 時砍半保新，避免無上限成長。失敗安靜放棄（不擋 append）。"""
    try:
        if p.stat().st_size <= _MAX_BYTES:
            return
        lines = p.read_text(encoding="utf-8").splitlines()
        keep = lines[len(lines) // 2 :]
        tmp = p.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(p)  # atomic
    except Exception:
        pass
