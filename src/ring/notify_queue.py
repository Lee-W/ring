"""通知合流（debounce）＋暫時 quiet mode 的所有檔案狀態操作。

跨 process 沒有常駐 daemon，這裡是「被 hold 住的通知」唯一的落地層：hook 事件（每次都是
一次性 subprocess）與 TUI 輪詢都經由這個模組讀寫同一份磁碟狀態，達成跨 process 合流。

機制概述
---------
- ``~/.config/ring/notify-queue.json``：debounce 視窗狀態（``window_opened_at``）＋被 hold
  住的 session 清單（去重，key 是 session_id）。debounce 與 quiet 共用同一份 queue——
  不論被 hold 的原因是哪個，flush 邏輯一致（見 ``flush_if_due``）。
- ``~/.config/ring/quiet``：暫時全域靜音狀態，``{"until": <epoch|null>, "since": <epoch>}``。
  ``until=None`` 代表手動解除前一直靜音；``until=<epoch>`` 到期即視為非 active（讀時判定，
  跟 ``ipc.py`` 的 TTL 到期即失效同一套風格）。

設計原則（仿 ``ipc.py`` / ``registry.py`` 既有 pattern）
---------------------------------------------------------
- 純 stdlib，零新依賴。
- 跨 process 的 read-modify-write 一律用 ``fcntl.flock`` 保護（抄 ``registry._hidden_sessions_lock``）。
- 所有檔案操作失敗安靜吞掉，不打斷 hook / TUI 主流程。
- 路徑常數可在測試中用關鍵字參數注入，隔離測試環境。
"""

from __future__ import annotations

import fcntl
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from ring.config import get_config
from ring.i18n import gettext as _
from ring.registry import Session, Status

_CONFIG_DIR: Path = Path.home() / ".config" / "ring"
_QUEUE_PATH: Path = _CONFIG_DIR / "notify-queue.json"
_QUIET_PATH: Path = _CONFIG_DIR / "quiet"


# --------------------------------------------------------------------------- 共用鎖 ＋ 讀寫 helper


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """跨 process 的 read-modify-write 臨界區（抄 ``registry._hidden_sessions_lock``）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_json_locked(path: Path) -> dict[str, Any]:
    """呼叫端須已持有 ``_locked(path)``。壞檔／缺檔一律視為空狀態，不拋例外。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return raw
    except Exception:
        pass
    return {}


def _write_json_locked(data: dict[str, Any], *, path: Path) -> None:
    """呼叫端須已持有 ``_locked(path)``。atomic tmp+replace，失敗安靜吞掉。"""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


# --------------------------------------------------------------------------- Session ↔ dict


def _session_to_dict(session: Session) -> dict[str, Any]:
    d = asdict(session)
    d["status"] = session.status.value
    return d


def _session_from_dict(d: dict[str, Any]) -> Session | None:
    """壞資料（缺必要欄位／型別不對）安靜跳過，回傳 ``None``。"""
    data = dict(d)
    try:
        data["status"] = Status(data.get("status", Status.WAITING.value))
    except ValueError:
        return None
    todo = data.get("todo")
    if isinstance(todo, list) and len(todo) == 2:
        data["todo"] = (todo[0], todo[1])
    # 未知欄位（例如舊版留下的）會讓 Session(**data) 直接炸；只保留 Session 認得的欄位。
    known = {f for f in Session.__dataclass_fields__}
    data = {k: v for k, v in data.items() if k in known}
    try:
        return Session(**data)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- queue（enqueue / pop_all / peek_count）


def enqueue(sessions: list[Session], *, queue_path: Path | None = None) -> None:
    """把一批 session 併入 queue（以 session_id 去重合併，新資料覆蓋舊的）。"""
    if not sessions:
        return
    path = queue_path or _QUEUE_PATH
    with _locked(path):
        state = _read_json_locked(path)
        bucket = state.get("sessions")
        if not isinstance(bucket, dict):
            bucket = {}
        for s in sessions:
            bucket[s.session_id] = _session_to_dict(s)
        state["sessions"] = bucket
        _write_json_locked(state, path=path)


def pop_all(*, queue_path: Path | None = None) -> list[Session]:
    """取出 queue 裡全部 session 並清空（消費即焚）。"""
    path = queue_path or _QUEUE_PATH
    with _locked(path):
        state = _read_json_locked(path)
        bucket = state.get("sessions")
        result: list[Session] = []
        if isinstance(bucket, dict):
            for raw in bucket.values():
                if isinstance(raw, dict):
                    s = _session_from_dict(raw)
                    if s is not None:
                        result.append(s)
        state["sessions"] = {}
        _write_json_locked(state, path=path)
    return result


def peek_count(*, queue_path: Path | None = None) -> int:
    """唯讀計數，不清空——給視覺化（TUI header badge）用。"""
    path = queue_path or _QUEUE_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    if not isinstance(raw, dict):
        return 0
    bucket = raw.get("sessions")
    return len(bucket) if isinstance(bucket, dict) else 0


# --------------------------------------------------------------------------- debounce 視窗狀態


def try_claim_leading_edge(now: float, seconds: float, *, queue_path: Path | None = None) -> bool:
    """原子版「視窗是否開著」判斷 ＋「開新視窗」動作，兩者包在同一個鎖區塊內。

    ``window_open()`` 接著呼叫 ``open_window()`` 是兩次獨立的 ``_locked()`` 加解鎖，中間
    沒有鎖保護——兩個 process 同時在「視窗未開」那個瞬間各自呼叫，會同時判定自己是
    leading edge、都照常發送（debounce 對這輪完全失效）。這個函式把 check-then-act 併進
    單一臨界區，保證同一視窗只有一個呼叫端能拿到 leading edge。

    :returns: ``True`` 代表這次呼叫拿到 leading edge（視窗未開，已原子性地幫它開新視窗，
        呼叫端應該照常發送這批）；``False`` 代表已經有人在視窗內（呼叫端應該 enqueue，不發）。
    """
    path = queue_path or _QUEUE_PATH
    with _locked(path):
        state = _read_json_locked(path)
        opened_at = state.get("window_opened_at")
        is_open = isinstance(opened_at, (int, float)) and (now - float(opened_at)) < seconds
        if is_open:
            return False
        state["window_opened_at"] = now
        _write_json_locked(state, path=path)
        return True


# --------------------------------------------------------------------------- quiet 狀態


def set_quiet(until: float | None, *, quiet_path: Path | None = None) -> None:
    """開啟 quiet：``until=None`` 手動解除前一直靜音；``until=<epoch>`` 到期自動解除。"""
    path = quiet_path or _QUIET_PATH
    payload: dict[str, Any] = {"until": until, "since": time.time()}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def clear_quiet(*, quiet_path: Path | None = None) -> None:
    """手動解除 quiet（``ring quiet off``）。"""
    path = quiet_path or _QUIET_PATH
    try:
        path.unlink()
    except OSError:
        pass


def _read_quiet(*, quiet_path: Path | None = None, now: float) -> dict[str, Any] | None:
    """讀 quiet 狀態；到期（``until`` 已過）視為非 active，順手刪掉 stale 檔（抄 ipc TTL 風格）。"""
    path = quiet_path or _QUIET_PATH
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    until = raw.get("until")
    if until is not None and isinstance(until, (int, float)) and now >= float(until):
        try:
            path.unlink()
        except OSError:
            pass
        return None
    return raw


def quiet_active(now: float, *, quiet_path: Path | None = None) -> bool:
    """quiet 目前是否生效（含到期即視為非 active 的判定）。"""
    return _read_quiet(quiet_path=quiet_path, now=now) is not None


def quiet_remaining(now: float, *, quiet_path: Path | None = None) -> float | None:
    """剩餘秒數；quiet 未開啟／已過期 → ``None``；``until=None``（無限期）也回 ``None``。"""
    data = _read_quiet(quiet_path=quiet_path, now=now)
    if data is None:
        return None
    until = data.get("until")
    if until is None or not isinstance(until, (int, float)):
        return None
    remaining = float(until) - now
    return remaining if remaining > 0 else None


def format_remaining(seconds: float) -> str:
    """人類可讀的剩餘時間，供 ``ring quiet`` CLI 與 TUI header badge 共用。"""
    total = max(0, int(seconds))
    if total < 60:
        return _("{s} 秒", s=total)
    minutes = total // 60
    if minutes < 60:
        return _("{m} 分", m=minutes)
    hours = minutes // 60
    return _("{h} 小時", h=hours)


# --------------------------------------------------------------------------- flush


def _try_pop_for_flush_locked(now: float, *, force: bool, queue_path: Path) -> list[Session] | None:
    """單一鎖區塊內原子完成「該不該 flush」的判斷 ＋ pop ＋ 清視窗。

    拆成「先 window_open() 判斷、再各自 pop_all()／clear_window()」的多段式會有縫隙：
    process H 判定「視窗已過期」到它真的 pop+clear 之間，若另一個 process W 恰好在這個
    縫隙 open_window() 開了新視窗（合法的下一輪 leading edge），H 的 clear_window() 會
    把 W 剛開的視窗殺掉——這個函式把「讀狀態→判斷是否該 flush→pop→清視窗」全部包進
    同一個 ``_locked()``，杜絕這個縫隙。

    :returns: ``None`` 代表這次不該 flush（非 force 且仍在 debounce 視窗內，或 queue 本來
        就空）；非 ``None`` 的 list（已從 queue 原子性地取出並清空）代表這次真的 flush 了。
    """
    with _locked(queue_path):
        state = _read_json_locked(queue_path)
        if not force:
            debounce = get_config().notify_debounce_seconds
            opened_at = state.get("window_opened_at")
            still_open = debounce > 0 and isinstance(opened_at, (int, float)) and (now - float(opened_at)) < debounce
            if still_open:
                return None

        bucket = state.get("sessions")
        sessions: list[Session] = []
        if isinstance(bucket, dict):
            for raw in bucket.values():
                if isinstance(raw, dict):
                    s = _session_from_dict(raw)
                    if s is not None:
                        sessions.append(s)
        if not sessions:
            return None

        state["sessions"] = {}
        state["window_opened_at"] = None
        _write_json_locked(state, path=queue_path)
        return sessions


def flush_if_due(
    *,
    now: float | None = None,
    force: bool = False,
    queue_path: Path | None = None,
    quiet_path: Path | None = None,
) -> None:
    """三個懶惰觸發源共用的 flush 入口：hook 主流程開頭、TUI 輪詢、``ring quiet off``。

    - quiet active 時一律不 flush（``force=True`` 除外）——quiet 期間累積，等使用者主動
      解除才處理。
    - 非 force：只有「不在 debounce 視窗內」時才 flush（``notify_debounce_seconds<=0`` 時
      視窗永遠不會開啟，等同一律可 flush，讓純 quiet 累積的項目一樣能被懶惰觸發清掉）。
    - force=True（``ring quiet off``）：跳過 quiet／視窗判斷，queue 有東西就直接 flush。

    「該不該 flush」的判斷與「pop＋清視窗」的動作是同一個鎖區塊內完成的原子操作（見
    ``_try_pop_for_flush_locked``），flush 完才呼叫 ``ring.notify.notify_summary`` 發一則
    彙總——延後 import 避免 notify_queue ↔ notify 循環依賴。失敗安靜吞掉，絕不擋住
    hook / TUI 主流程。
    """
    current = now if now is not None else time.time()
    if not force and quiet_active(current, quiet_path=quiet_path):
        return

    path = queue_path or _QUEUE_PATH
    sessions = _try_pop_for_flush_locked(current, force=force, queue_path=path)
    if not sessions:
        return
    try:
        from ring.notify import notify_summary

        notify_summary(len(sessions), sessions[0])
    except Exception:
        pass
