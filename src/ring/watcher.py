"""不依賴 Textual 的「新轉 waiting 偵測器」狀態件。

讓 TUI 與 headless watch() 兩條 loop 共用同一套「某 session 新轉為 waiting」邏輯，
避免維護兩套可能打架的差異邏輯。完全不 import Textual，可在無 macOS GUI 環境單測。
"""

from __future__ import annotations

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
