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

    ``cooldown_seconds``：session 離開 WAITING 後若很快又轉回（例如背景 subagent 的權限
    請求在 working/waiting 間快速翻轉），距上次通知未滿冷卻期就不當「新轉入」立即通知——
    否則翻轉一次響一次。冷卻期只抑制「重新轉入」這一次的通知，state 仍照常建立，冷卻期滿
    後的 repeat 邏輯不受影響。0 = 關閉（回到「離開就丟狀態、再轉入必響」的舊行為）。
    """

    def __init__(
        self,
        repeat_seconds: tuple[int, ...] = (),
        repeat_max: int = 0,
        *,
        now: Callable[[], float] = time.time,
        cooldown_seconds: int = 0,
    ) -> None:
        self._repeat_seconds = repeat_seconds
        self._repeat_max = max(0, repeat_max)
        self._now = now
        self._cooldown_seconds = max(0, cooldown_seconds)
        self._states: dict[str, _AlertState] = {}
        # sid → 上次通知時間；離開 WAITING 後 self._states 會丟掉該 sid，但這裡保留給
        # 冷卻判斷用。只增不減地跟著 feed() 清理過期條目，避免無界成長。
        self._recently_alerted: dict[str, float] = {}
        self._primed = False

    def feed(self, sessions: list[Session]) -> list[Session]:
        current = {s.session_id: s for s in sessions if s.status is Status.WAITING}
        now = self._now()

        if not self._primed:
            self._states = {sid: _AlertState(first_seen=now, last_alert=now) for sid in current}
            self._recently_alerted = {sid: now for sid in current}
            self._primed = True
            return []

        due: list[Session] = []
        next_states: dict[str, _AlertState] = {}
        for sid, session in current.items():
            state = self._states.get(sid)
            if state is None:
                last_alert = self._recently_alerted.get(sid)
                if self._cooldown_seconds > 0 and last_alert is not None and now - last_alert < self._cooldown_seconds:
                    # 冷卻中：不通知，但仍建立 state 追蹤，冷卻期滿後 repeat 邏輯照舊接手。
                    next_states[sid] = _AlertState(first_seen=now, last_alert=last_alert)
                else:
                    next_states[sid] = _AlertState(first_seen=now, last_alert=now)
                    self._recently_alerted[sid] = now
                    due.append(session)
                continue

            if self._should_repeat(state, now):
                state.last_alert = now
                state.repeats_sent += 1
                due.append(session)
                self._recently_alerted[sid] = now
            next_states[sid] = state

        self._states = next_states
        self._prune_recently_alerted(now)
        return due

    def _prune_recently_alerted(self, now: float) -> None:
        """清掉超過冷卻期的 recently-alerted 條目，避免長跑 process 無界成長。"""
        if not self._cooldown_seconds:
            self._recently_alerted.clear()
            return
        cutoff = now - self._cooldown_seconds
        self._recently_alerted = {sid: t for sid, t in self._recently_alerted.items() if t >= cutoff}

    def _should_repeat(self, state: _AlertState, now: float) -> bool:
        if not self._repeat_seconds:
            return False
        if self._repeat_max and state.repeats_sent >= self._repeat_max:
            return False
        if state.repeats_sent < len(self._repeat_seconds):
            return now - state.first_seen >= self._repeat_seconds[state.repeats_sent]
        return now - state.last_alert >= self._repeat_seconds[-1]
