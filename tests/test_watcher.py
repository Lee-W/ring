"""WaitingWatcher 純單測——完全不碰 Textual / macOS GUI。"""

from __future__ import annotations

from ring.registry import Session, Status
from ring.watcher import WaitingAlertScheduler, WaitingWatcher


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


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TestWaitingAlertScheduler:
    def test_prime_does_not_alert_existing_waiting(self) -> None:
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock)

        assert scheduler.feed([_s("a", Status.WAITING)]) == []

    def test_new_waiting_alerts_immediately_after_prime(self) -> None:
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock)
        scheduler.feed([_s("a", Status.WORKING)])

        result = scheduler.feed([_s("a", Status.WAITING)])

        assert [s.session_id for s in result] == ["a"]

    def test_persistent_waiting_repeats_after_threshold(self) -> None:
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock)
        scheduler.feed([_s("a", Status.WAITING)])

        clock.advance(29)
        assert scheduler.feed([_s("a", Status.WAITING)]) == []
        clock.advance(1)
        result = scheduler.feed([_s("a", Status.WAITING)])

        assert [s.session_id for s in result] == ["a"]

    def test_repeat_max_limits_repeats(self) -> None:
        clock = _Clock()
        scheduler = WaitingAlertScheduler((10,), 1, now=clock)
        scheduler.feed([_s("a", Status.WAITING)])

        clock.advance(10)
        assert len(scheduler.feed([_s("a", Status.WAITING)])) == 1
        clock.advance(10)
        assert scheduler.feed([_s("a", Status.WAITING)]) == []

    def test_leaving_waiting_resets_state(self) -> None:
        """cooldown_seconds 預設 0（關閉）→ 保留舊行為：離開再轉回立即通知。"""
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock)
        scheduler.feed([_s("a", Status.WAITING)])
        scheduler.feed([_s("a", Status.WORKING)])

        result = scheduler.feed([_s("a", Status.WAITING)])

        assert [s.session_id for s in result] == ["a"]

    def test_cooldown_suppresses_realert_when_flapping_back_into_waiting(self) -> None:
        """反例：權限請求快速在 working/waiting 間翻轉——冷卻期內轉回 waiting 不該再響一次。

        還原成沒有冷卻期的版本（不傳 cooldown_seconds）會在這裡紅：翻轉一次響一次。
        """
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock, cooldown_seconds=180)
        scheduler.feed([_s("a", Status.WORKING)])  # prime

        result1 = scheduler.feed([_s("a", Status.WAITING)])
        assert [s.session_id for s in result1] == ["a"]

        clock.advance(20)
        scheduler.feed([_s("a", Status.WORKING)])  # 翻轉：離開 waiting（PermissionRequest 解決）
        clock.advance(5)
        result2 = scheduler.feed([_s("a", Status.WAITING)])  # 25 秒後又翻回 waiting，仍在冷卻期內

        assert result2 == [], "冷卻期內重新轉入 waiting 不該立即再通知"

    def test_realert_after_cooldown_expires(self) -> None:
        """冷卻期滿後才轉回 waiting → 視為真正的新轉入，照常立即通知。"""
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock, cooldown_seconds=180)
        scheduler.feed([_s("a", Status.WORKING)])  # prime
        scheduler.feed([_s("a", Status.WAITING)])

        clock.advance(90)
        scheduler.feed([_s("a", Status.WORKING)])
        clock.advance(181)  # 冷卻期滿（累計早已 > 180 秒）
        result = scheduler.feed([_s("a", Status.WAITING)])

        assert [s.session_id for s in result] == ["a"]

    def test_cooldown_tracks_state_and_repeat_logic_resumes_after_suppressed_realert(self) -> None:
        """冷卻期內雖不通知，仍要建立 state 追蹤；持續 waiting 到 repeat 門檻照樣提醒。"""
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock, cooldown_seconds=180)
        scheduler.feed([_s("a", Status.WORKING)])  # prime
        scheduler.feed([_s("a", Status.WAITING)])

        clock.advance(10)
        scheduler.feed([_s("a", Status.WORKING)])
        clock.advance(5)
        suppressed = scheduler.feed([_s("a", Status.WAITING)])  # 冷卻期內，抑制
        assert suppressed == []

        clock.advance(30)  # 距這次「轉入」的 first_seen 已過 repeat_seconds[0]=30
        result = scheduler.feed([_s("a", Status.WAITING)])

        assert [s.session_id for s in result] == ["a"]

    def test_cooldown_zero_keeps_legacy_behavior_explicitly(self) -> None:
        """cooldown_seconds=0 明確關閉冷卻——即使剛通知過，立刻轉回也照樣立即再通知。"""
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock, cooldown_seconds=0)
        scheduler.feed([_s("a", Status.WORKING)])  # prime
        scheduler.feed([_s("a", Status.WAITING)])
        scheduler.feed([_s("a", Status.WORKING)])

        result = scheduler.feed([_s("a", Status.WAITING)])

        assert [s.session_id for s in result] == ["a"]

    def test_recently_alerted_table_is_pruned_after_cooldown_window(self) -> None:
        """recently-alerted 表要隨冷卻期過期清理，不無界成長（多 session 長跑情境）。"""
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock, cooldown_seconds=60)
        scheduler.feed([])  # prime

        for i in range(50):
            sid = f"s{i}"
            scheduler.feed([_s(sid, Status.WAITING)])
            scheduler.feed([_s(sid, Status.WORKING)])

        # 早就超過 60 秒冷卻期，之前累積的條目該被清掉，不會一路累加到 50 筆。
        clock.advance(1000)
        scheduler.feed([_s("tail", Status.WAITING)])

        assert len(scheduler._recently_alerted) < 50

    def test_batched_alerts_returned_as_single_list_for_one_tick(self) -> None:
        """同一輪 tick 多個 session 同時 due → 一次 feed() 回傳含所有名字的清單（合批），不是逐一回傳。"""
        clock = _Clock()
        scheduler = WaitingAlertScheduler((30,), 1, now=clock)
        scheduler.feed([_s("a", Status.WORKING), _s("b", Status.WORKING)])  # prime

        result = scheduler.feed([_s("a", Status.WAITING), _s("b", Status.WAITING)])

        assert {s.session_id for s in result} == {"a", "b"}
