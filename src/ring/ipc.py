"""檔案式 IPC 輔助——RiNG TUI 與 ``ring focus`` subprocess 之間的溝通橋梁。

機制概述
---------
- ``~/.config/ring/focus-request``：由 ``ring focus <id>`` 寫入、TUI poll 後讀取並刪除（消費即焚）。
- ``~/.config/ring/tui-presence``：由 RiNG TUI 啟動時寫入、結束時刪除，同時做「TUI 是否在跑」的判斷依據。

設計原則
---------
- 純 stdlib，零新依賴。
- 所有跨 process 檔案操作包 try/except，失敗安靜吞掉，不打斷 TUI / 通知主流程。
- ``_config_dir``、``_focus_request_path``、``_presence_path`` 為可在測試中注入的私有常數，
  使用者透過 ``tmp_path`` 覆寫即可隔離測試環境。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import TypedDict

# --------------------------------------------------------------------------- 路徑常數（可在測試中覆寫）

_CONFIG_DIR: Path = Path.home() / ".config" / "ring"
_FOCUS_REQUEST_PATH: Path = _CONFIG_DIR / "focus-request"
_PRESENCE_PATH: Path = _CONFIG_DIR / "tui-presence"

# focus-request 的有效時效（秒）：超過視為 stale，忽略。
_REQUEST_TTL: float = 30.0

# presence 的有效時效（秒）：超過視為 stale，即使 pid 看起來還活著也不信任。
_PRESENCE_TTL: float = 300.0


# --------------------------------------------------------------------------- 型別


class _FocusRequest(TypedDict):
    session_id: str
    ts: float


class _TuiPresence(TypedDict):
    tty: str
    pid: int
    ts: float


# --------------------------------------------------------------------------- focus-request 相關


def write_focus_request(session_id: str, *, request_path: Path | None = None) -> None:
    """把 focus-request 寫入磁碟，供 TUI poll 讀取。

    :param session_id: 要聚焦的 session ID。
    :param request_path: 可注入路徑（測試用）；省略時用預設路徑。
    """
    path = request_path or _FOCUS_REQUEST_PATH
    payload: _FocusRequest = {"session_id": session_id, "ts": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def read_focus_request(
    *,
    request_path: Path | None = None,
    ttl: float = _REQUEST_TTL,
) -> str | None:
    """讀取 focus-request 並消費即焚。

    - 無檔案 → 回傳 ``None``，不刪（無檔可刪）。
    - 無效 JSON → 刪掉爛檔，回傳 ``None``。
    - 過期（超過 ttl 秒）→ 刪檔，回傳 ``None``。
    - 有效 request → 刪檔，回傳 ``session_id``。

    :param request_path: 可注入路徑（測試用）。
    :param ttl: 有效時效秒數，超過視為 stale。
    :returns: session_id 或 None。
    """
    path = request_path or _FOCUS_REQUEST_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None

    try:
        data: _FocusRequest = json.loads(raw)
        session_id = str(data["session_id"])
        ts = float(data["ts"])
    except Exception:
        # 解析失敗：刪掉爛檔
        try:
            path.unlink()
        except Exception:
            pass
        return None

    age = time.time() - ts
    # 不論有效或過期，都刪掉（消費即焚；過期的也不留）
    try:
        path.unlink()
    except Exception:
        pass

    if age > ttl:
        return None

    return session_id


# --------------------------------------------------------------------------- tui-presence 相關


def _pid_alive(pid: int) -> bool:
    """檢查 pid 是否仍存活（送 signal 0）。"""
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
    except Exception:
        return False


def write_tui_presence(*, presence_path: Path | None = None) -> None:
    """將目前 process 的 tty / pid / ts 寫入 presence 檔。

    tty 取自 controlling terminal（``os.ttyname(sys.stdout.fileno())`` 或類似）。
    若無法取得 tty（非互動終端）則寫空字串，讓 activate 步驟跳過而不崩。

    :param presence_path: 可注入路徑（測試用）。
    """
    path = presence_path or _PRESENCE_PATH
    tty = ""
    try:
        # 優先用 stdout；若 stdout 非 tty，嘗試直接開 /dev/tty
        if sys.stdout.isatty():
            tty = os.ttyname(sys.stdout.fileno())
        else:
            fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
            try:
                tty = os.ttyname(fd)
            finally:
                os.close(fd)
    except Exception:
        pass  # headless / no controlling terminal，tty 留空字串

    payload: _TuiPresence = {"tty": tty, "pid": os.getpid(), "ts": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass


def read_tui_presence(
    *,
    presence_path: Path | None = None,
    ttl: float = _PRESENCE_TTL,
) -> _TuiPresence | None:
    """讀取 presence 檔，判斷 TUI 是否確實在跑。

    判定規則：
    - 無檔案、無效 JSON → ``None``。
    - ts 超過 ttl → stale，刪檔並回傳 ``None``。
    - pid 已死 → stale，刪檔並回傳 ``None``。
    - 以上均通過 → 回傳 presence dict。

    :param presence_path: 可注入路徑（測試用）。
    :param ttl: 有效時效秒數。
    :returns: ``_TuiPresence`` 或 ``None``。
    """
    path = presence_path or _PRESENCE_PATH
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:
        return None

    try:
        data: _TuiPresence = json.loads(raw)
        tty = str(data.get("tty", ""))
        pid = int(data["pid"])
        ts = float(data["ts"])
    except Exception:
        return None

    age = time.time() - ts
    if age > ttl:
        try:
            path.unlink()
        except Exception:
            pass
        return None

    if not _pid_alive(pid):
        try:
            path.unlink()
        except Exception:
            pass
        return None

    return _TuiPresence(tty=tty, pid=pid, ts=ts)


def clear_tui_presence(*, presence_path: Path | None = None) -> None:
    """刪除 presence 檔（TUI 離場時呼叫）。

    :param presence_path: 可注入路徑（測試用）。
    """
    path = presence_path or _PRESENCE_PATH
    try:
        path.unlink()
    except Exception:
        pass
