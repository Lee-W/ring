"""不依賴 Textual 的「新轉 waiting 偵測器」狀態件。

讓 TUI 與 headless watch() 兩條 loop 共用同一套「某 session 新轉為 waiting」邏輯，
避免維護兩套可能打架的差異邏輯。完全不 import Textual，可在無 macOS GUI 環境單測。
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ring.registry import Session, Status


class WaitingWatcher:
    """追蹤 session snapshot 差集，回傳「本輪新轉為 waiting」的清單。

    第一輪（prime 語意）：只記錄當前 waiting set、回空清單，避免啟動瞬間把所有既有
    waiting session 當「新轉」狂發通知。第二輪起才開始吐差集。
    """

    def __init__(self) -> None:
        self._waiting_ids: set[str] = set()
        self._primed: bool = False

    def feed(self, sessions: list[Session]) -> list[Session]:
        """餵入當前 snapshot，回傳「本輪新轉為 waiting」的 Session 清單。

        :param sessions: 當前完整 session 快照（通常來自 board() / discover_sessions()）。
        :returns: 本輪從「非 waiting」轉為「waiting」的 session；第一輪一律回空清單。
        """
        current_waiting = {s.session_id for s in sessions if s.status is Status.WAITING}
        if not self._primed:
            # 第一輪只記錄，不發通知
            self._waiting_ids = current_waiting
            self._primed = True
            return []
        newly = current_waiting - self._waiting_ids
        self._waiting_ids = current_waiting
        if not newly:
            return []
        return [s for s in sessions if s.session_id in newly]


@dataclass
class _AlertState:
    first_seen: float
    last_alert: float
    repeats_sent: int = 0


class WaitingAlertScheduler:
    """決定哪些 waiting session 此輪需要通知。

    第一輪只 prime，不通知既有 waiting；之後新轉 waiting 立即通知，持續 waiting 則依
    ``repeat_seconds`` 做最多 ``repeat_max`` 次提醒（0 = 不限）。
    """

    def __init__(
        self,
        repeat_seconds: tuple[int, ...] = (),
        repeat_max: int = 0,
        *,
        now: Callable[[], float] = time.time,
    ) -> None:
        self._repeat_seconds = repeat_seconds
        self._repeat_max = max(0, repeat_max)
        self._now = now
        self._states: dict[str, _AlertState] = {}
        self._primed = False

    def feed(self, sessions: list[Session]) -> list[Session]:
        current = {s.session_id: s for s in sessions if s.status is Status.WAITING}
        now = self._now()

        if not self._primed:
            self._states = {sid: _AlertState(first_seen=now, last_alert=now) for sid in current}
            self._primed = True
            return []

        due: list[Session] = []
        next_states: dict[str, _AlertState] = {}
        for sid, session in current.items():
            state = self._states.get(sid)
            if state is None:
                next_states[sid] = _AlertState(first_seen=now, last_alert=now)
                due.append(session)
                continue

            if self._should_repeat(state, now):
                state.last_alert = now
                state.repeats_sent += 1
                due.append(session)
            next_states[sid] = state

        self._states = next_states
        return due

    def _should_repeat(self, state: _AlertState, now: float) -> bool:
        if not self._repeat_seconds:
            return False
        if self._repeat_max and state.repeats_sent >= self._repeat_max:
            return False
        if state.repeats_sent < len(self._repeat_seconds):
            return now - state.first_seen >= self._repeat_seconds[state.repeats_sent]
        return now - state.last_alert >= self._repeat_seconds[-1]
