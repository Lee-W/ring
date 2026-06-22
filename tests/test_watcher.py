"""WaitingWatcher 純單測——完全不碰 Textual / macOS GUI。"""

from __future__ import annotations

from ring.registry import Session, Status
from ring.watcher import WaitingWatcher


def _s(sid: str, status: Status) -> Session:
    return Session(sid, f"/x/{sid}", status, 0.0, "→ Edit", "hook")


class TestWaitingWatcherPrime:
    def test_prime_returns_empty_even_with_multiple_waiting(self) -> None:
        """第一輪即使已有多個 waiting，回空清單（不狂發）。"""
        watcher = WaitingWatcher()
        sessions = [_s("a", Status.WAITING), _s("b", Status.WAITING), _s("c", Status.WORKING)]
        result = watcher.feed(sessions)
        assert result == []

    def test_prime_empty_snapshot(self) -> None:
        """第一輪空 snapshot → 也回空。"""
        watcher = WaitingWatcher()
        assert watcher.feed([]) == []


class TestWaitingWatcherDiff:
    def test_new_waiting_returned_on_transition(self) -> None:
        """某 session 由非 waiting 轉 waiting → 那輪回傳它。"""
        watcher = WaitingWatcher()
        # prime
        watcher.feed([_s("a", Status.WORKING)])
        # 轉 waiting
        result = watcher.feed([_s("a", Status.WAITING)])
        assert len(result) == 1
        assert result[0].session_id == "a"

    def test_persistent_waiting_not_repeated(self) -> None:
        """持續 waiting 的 session 不重複回傳。"""
        watcher = WaitingWatcher()
        # prime
        watcher.feed([_s("a", Status.WORKING)])
        # 第一次轉 waiting → 回傳
        watcher.feed([_s("a", Status.WAITING)])
        # 持續 waiting → 不再回傳
        result = watcher.feed([_s("a", Status.WAITING)])
        assert result == []

    def test_returns_empty_when_no_new_waiting(self) -> None:
        """無新轉 waiting → 回空清單。"""
        watcher = WaitingWatcher()
        watcher.feed([_s("a", Status.WORKING)])
        result = watcher.feed([_s("a", Status.WORKING)])
        assert result == []

    def test_transition_back_and_forth(self) -> None:
        """轉出再轉回 waiting → 再次回傳。"""
        watcher = WaitingWatcher()
        # prime
        watcher.feed([_s("a", Status.WORKING)])
        # 轉 waiting
        result1 = watcher.feed([_s("a", Status.WAITING)])
        assert len(result1) == 1
        # 轉回 working（不在 waiting）
        watcher.feed([_s("a", Status.WORKING)])
        # 再轉 waiting → 再次回傳
        result2 = watcher.feed([_s("a", Status.WAITING)])
        assert len(result2) == 1
        assert result2[0].session_id == "a"

    def test_multiple_sessions_only_new_ones_returned(self) -> None:
        """多個 session 同一輪，只有「新轉」的那些被回傳。"""
        watcher = WaitingWatcher()
        # prime with one waiting, one working
        watcher.feed([_s("a", Status.WAITING), _s("b", Status.WORKING)])
        # b 轉 waiting；a 仍 waiting（prime 後第一輪，a 在 _waiting_ids 裡不算新）
        result = watcher.feed([_s("a", Status.WAITING), _s("b", Status.WAITING)])
        assert len(result) == 1
        assert result[0].session_id == "b"

    def test_empty_snapshot_after_prime(self) -> None:
        """prime 後空 snapshot → 回空（無 session 進 waiting）。"""
        watcher = WaitingWatcher()
        watcher.feed([_s("a", Status.WAITING)])
        result = watcher.feed([])
        assert result == []
