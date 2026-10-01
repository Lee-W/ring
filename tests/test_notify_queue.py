"""notify_queue.py 單元測試——debounce queue、視窗狀態、quiet 狀態、flush_if_due。"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from ring.config import Config
from ring.notify_queue import (
    clear_quiet,
    enqueue,
    flush_if_due,
    format_remaining,
    peek_count,
    pop_all,
    quiet_active,
    quiet_remaining,
    set_quiet,
    try_claim_leading_edge,
)
from ring.registry import Session, Status
from ring.waiting import WaitingRequest


def _s(sid: str, project: str = "proj") -> Session:
    return Session(sid, f"/x/{project}", Status.WAITING, 0.0, "→ Edit", "hook", origin_cwd=f"/x/{project}")


# --------------------------------------------------------------------------- enqueue / pop_all / peek_count


class TestQueue:
    def test_enqueue_dedupes_by_session_id(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a", "p1")], queue_path=q)
        enqueue([_s("a", "p1"), _s("b", "p2")], queue_path=q)
        result = pop_all(queue_path=q)
        assert {s.session_id for s in result} == {"a", "b"}

    def test_pop_all_consumes_queue(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a")], queue_path=q)
        first = pop_all(queue_path=q)
        second = pop_all(queue_path=q)
        assert len(first) == 1
        assert second == []

    def test_peek_count_does_not_clear(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a"), _s("b")], queue_path=q)
        assert peek_count(queue_path=q) == 2
        assert peek_count(queue_path=q) == 2  # 唯讀，不清空

    def test_peek_count_missing_file_is_zero(self, tmp_path: Path) -> None:
        assert peek_count(queue_path=tmp_path / "nope.json") == 0

    def test_enqueue_empty_list_is_noop(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([], queue_path=q)
        assert not q.exists()

    def test_pop_all_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert pop_all(queue_path=tmp_path / "nope.json") == []

    def test_enqueue_survives_corrupt_file(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        q.write_text("not json", encoding="utf-8")
        enqueue([_s("a")], queue_path=q)
        assert peek_count(queue_path=q) == 1

    def test_round_trip_preserves_status_and_project(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a", "maigo")], queue_path=q)
        result = pop_all(queue_path=q)
        assert result[0].status is Status.WAITING
        assert result[0].project == "maigo"

    def test_concurrent_enqueue_does_not_lose_updates(self, tmp_path: Path) -> None:
        """兩個 thread 同時 enqueue 不同 session → flock 保護，兩筆都要進 queue（不 lost-update）。"""
        q = tmp_path / "queue.json"

        def _enqueue_many(prefix: str) -> None:
            for i in range(20):
                enqueue([_s(f"{prefix}-{i}")], queue_path=q)

        threads = [threading.Thread(target=_enqueue_many, args=(p,)) for p in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        result = pop_all(queue_path=q)
        assert len(result) == 40


# --------------------------------------------------------------------------- debounce 視窗


class TestTryClaimLeadingEdge:
    def test_first_call_claims_leading_edge(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        now = 1000.0
        assert try_claim_leading_edge(now, 30, queue_path=q) is True

    def test_second_call_within_window_does_not_claim(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        now = 1000.0
        try_claim_leading_edge(now, 30, queue_path=q)
        assert try_claim_leading_edge(now + 5, 30, queue_path=q) is False

    def test_call_after_window_expires_claims_again(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        now = 1000.0
        try_claim_leading_edge(now, 30, queue_path=q)
        assert try_claim_leading_edge(now + 31, 30, queue_path=q) is True

    def test_concurrent_calls_only_one_claims_leading_edge(self, tmp_path: Path) -> None:
        """Repro 對照組：Soyo 的 race_repro.py 打的是舊版 window_open()+open_window() 兩段式，
        兩個 thread 都會判定自己是 leading edge（count==2）。這裡改打真正的
        try_claim_leading_edge()，同一個鎖區塊內完成判斷＋宣告，count 必須是 1。"""
        q = tmp_path / "queue.json"
        now = time.time()
        barrier = threading.Barrier(8)
        claimed: list[bool] = []
        lock = threading.Lock()

        def worker() -> None:
            barrier.wait()  # 逼所有 thread 幾乎同時撞進 try_claim_leading_edge
            result = try_claim_leading_edge(now, 30, queue_path=q)
            with lock:
                claimed.append(result)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        leading_edge_count = sum(1 for c in claimed if c)
        assert leading_edge_count == 1


# --------------------------------------------------------------------------- quiet


class TestQuiet:
    def test_not_active_when_never_set(self, tmp_path: Path) -> None:
        assert quiet_active(time.time(), quiet_path=tmp_path / "quiet") is False

    def test_active_with_no_expiry(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        set_quiet(None, quiet_path=p)
        assert quiet_active(time.time(), quiet_path=p) is True

    def test_active_before_expiry(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        now = 1000.0
        set_quiet(now + 60, quiet_path=p)
        assert quiet_active(now + 30, quiet_path=p) is True

    def test_inactive_after_expiry(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        now = 1000.0
        set_quiet(now - 1, quiet_path=p)
        assert quiet_active(now, quiet_path=p) is False

    def test_expired_quiet_file_is_cleaned_up(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        now = 1000.0
        set_quiet(now - 1, quiet_path=p)
        quiet_active(now, quiet_path=p)
        assert not p.exists()

    def test_clear_quiet_removes_file(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        set_quiet(None, quiet_path=p)
        clear_quiet(quiet_path=p)
        assert quiet_active(time.time(), quiet_path=p) is False

    def test_clear_quiet_nonexistent_does_not_raise(self, tmp_path: Path) -> None:
        clear_quiet(quiet_path=tmp_path / "nope")  # 不應拋

    def test_quiet_remaining_with_expiry(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        now = 1000.0
        set_quiet(now + 90, quiet_path=p)
        remaining = quiet_remaining(now, quiet_path=p)
        assert remaining is not None
        assert 89 < remaining <= 90

    def test_quiet_remaining_none_when_indefinite(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        set_quiet(None, quiet_path=p)
        assert quiet_remaining(time.time(), quiet_path=p) is None

    def test_quiet_remaining_none_when_not_active(self, tmp_path: Path) -> None:
        assert quiet_remaining(time.time(), quiet_path=tmp_path / "nope") is None

    def test_set_quiet_writes_since(self, tmp_path: Path) -> None:
        p = tmp_path / "quiet"
        set_quiet(None, quiet_path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "since" in data


class TestFormatRemaining:
    def test_seconds(self) -> None:
        assert "30" in format_remaining(30)

    def test_minutes(self) -> None:
        assert "12" in format_remaining(12 * 60 + 5)

    def test_hours(self) -> None:
        assert "2" in format_remaining(2 * 3600 + 100)

    def test_negative_clamped_to_zero(self) -> None:
        format_remaining(-5)  # 不應拋


# --------------------------------------------------------------------------- flush_if_due


class TestFlushIfDue:
    @pytest.fixture(autouse=True)
    def _current_sessions(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            "ring.sources.discover_sessions", lambda: [_s(sid) for sid in ("a", "b", *[f"s{i}" for i in range(5)])]
        )

    def _cfg(self, debounce: int) -> Config:
        return Config(notify_debounce_seconds=debounce)

    def test_no_flush_when_quiet_active(self, tmp_path: Path) -> None:
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        enqueue([_s("a")], queue_path=q)
        set_quiet(None, quiet_path=quiet)
        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(5)),
            patch("ring.notify.notify_summary") as mock_summary,
        ):
            flush_if_due(now=time.time(), queue_path=q, quiet_path=quiet)
        mock_summary.assert_not_called()
        assert peek_count(queue_path=q) == 1  # queue 保留

    def test_no_flush_within_debounce_window(self, tmp_path: Path) -> None:
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        now = 1000.0
        try_claim_leading_edge(now, 1, queue_path=q)  # 開視窗（seconds 值在此無關緊要，只是設置起點）
        enqueue([_s("a")], queue_path=q)
        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(30)),
            patch("ring.notify.notify_summary") as mock_summary,
        ):
            flush_if_due(now=now + 5, queue_path=q, quiet_path=quiet)
        mock_summary.assert_not_called()

    def test_flushes_after_window_expires(self, tmp_path: Path) -> None:
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        now = 1000.0
        try_claim_leading_edge(now, 1, queue_path=q)  # 開視窗（seconds 值在此無關緊要，只是設置起點）
        enqueue([_s("a"), _s("b")], queue_path=q)
        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(10)),
            patch("ring.notify.notify_summary") as mock_summary,
        ):
            flush_if_due(now=now + 11, queue_path=q, quiet_path=quiet)
        mock_summary.assert_called_once()
        assert mock_summary.call_args[0][0] == 2
        assert peek_count(queue_path=q) == 0

    @pytest.mark.parametrize("status", [Status.WORKING, Status.IDLE, Status.ENDED, None])
    def test_resolved_or_gone_wait_is_discarded(self, tmp_path: Path, status: Status | None) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a")], queue_path=q)
        current = [] if status is None else [replace(_s("a"), status=status)]
        with patch("ring.notify.notify_summary") as summary:
            flush_if_due(force=True, queue_path=q, quiet_path=tmp_path / "quiet", current_sessions=current)
        summary.assert_not_called()
        assert peek_count(queue_path=q) == 0

    def test_request_revision_survives_round_trip(self, tmp_path: Path) -> None:
        request = WaitingRequest("foreground", "permission", "Edit", revision="request-1")
        session = replace(_s("a"), waiting_requests=(request,))
        q = tmp_path / "queue.json"
        enqueue([session], queue_path=q)
        assert pop_all(queue_path=q)[0].waiting_requests == (request,)

    def test_invalid_wait_owner_is_ignored(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a")], queue_path=q)
        raw = json.loads(q.read_text())
        raw["sessions"]["a"]["waiting_requests"] = [{"owner": []}, {"owner": "foreground", "revision": "valid"}]
        q.write_text(json.dumps(raw))
        assert pop_all(queue_path=q)[0].waiting_requests == (WaitingRequest("foreground", revision="valid"),)

    def test_replaced_request_is_not_notified_by_old_queue(self, tmp_path: Path) -> None:
        request = WaitingRequest("foreground", "permission", "Edit", revision="old")
        queued = replace(_s("a"), waiting_requests=(request,))
        current = replace(queued, waiting_requests=(replace(request, revision="new"),))
        q = tmp_path / "queue.json"
        enqueue([queued], queue_path=q)
        with patch("ring.notify.notify_summary") as summary:
            flush_if_due(force=True, queue_path=q, current_sessions=[current])
        summary.assert_not_called()
        assert peek_count(queue_path=q) == 0

    def test_same_request_uses_latest_location_and_detail(self, tmp_path: Path) -> None:
        request = WaitingRequest("foreground", "permission", "Edit", revision="same")
        queued = replace(_s("a"), waiting_requests=(request,))
        latest = replace(queued, last_active=100.0, tty="/dev/ttys002", waiting_detail="new detail")
        q = tmp_path / "queue.json"
        enqueue([queued], queue_path=q)
        with patch("ring.notify.notify_summary") as summary:
            flush_if_due(force=True, queue_path=q, current_sessions=[latest])
        summary.assert_called_once_with(1, latest)

    def test_another_agent_wait_does_not_keep_resolved_request(self, tmp_path: Path) -> None:
        host = WaitingRequest("foreground", revision="host")
        agent = WaitingRequest("agent:a", revision="agent")
        queued = replace(_s("a"), waiting_requests=(host,))
        q = tmp_path / "queue.json"
        enqueue([queued], queue_path=q)
        with patch("ring.notify.notify_summary") as summary:
            flush_if_due(force=True, queue_path=q, current_sessions=[replace(queued, waiting_requests=(agent,))])
        summary.assert_not_called()

    def test_discovery_failure_retains_queue(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a")], queue_path=q)
        with patch("ring.sources.discover_sessions", side_effect=RuntimeError("unavailable")):
            flush_if_due(force=True, queue_path=q)
        assert peek_count(queue_path=q) == 1

    def test_failed_source_keeps_only_uncertain_items(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        enqueue([_s("a"), _s("b"), _s("missing")], queue_path=q)
        with (
            patch("ring.sources.source_errors", return_value={"flaky": "unavailable"}),
            patch("ring.sources.stale_session_ids", return_value={"a"}),
            patch("ring.notify.notify_summary") as summary,
        ):
            flush_if_due(force=True, queue_path=q, current_sessions=[_s("a"), _s("b")])
        summary.assert_called_once_with(1, _s("b"))
        assert {s.session_id for s in pop_all(queue_path=q)} == {"a", "missing"}

    def test_enqueue_during_discovery_is_left_for_next_flush(self, tmp_path: Path) -> None:
        q = tmp_path / "queue.json"
        old = _s("a")
        new = replace(old, last_active=100.0)
        enqueue([old], queue_path=q)

        def discover() -> list[Session]:
            enqueue([new], queue_path=q)
            return [new]

        with (
            patch("ring.sources.discover_sessions", side_effect=discover),
            patch("ring.notify.notify_summary") as summary,
        ):
            flush_if_due(force=True, queue_path=q)
        summary.assert_not_called()
        assert peek_count(queue_path=q) == 1
        with patch("ring.notify.notify_summary") as summary:
            flush_if_due(force=True, queue_path=q, current_sessions=[new])
        summary.assert_called_once_with(1, new)

    def test_flushes_with_debounce_disabled_no_window_ever_opened(self, tmp_path: Path) -> None:
        """debounce=0（純 quiet 累積的殘留）——沒開過視窗也要能被懶惰 flush。"""
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        enqueue([_s("a")], queue_path=q)
        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(0)),
            patch("ring.notify.notify_summary") as mock_summary,
        ):
            flush_if_due(now=time.time(), queue_path=q, quiet_path=quiet)
        mock_summary.assert_called_once()

    def test_no_flush_when_queue_empty(self, tmp_path: Path) -> None:
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(0)),
            patch("ring.notify.notify_summary") as mock_summary,
        ):
            flush_if_due(now=time.time(), queue_path=q, quiet_path=quiet)
        mock_summary.assert_not_called()

    def test_force_bypasses_quiet_and_window(self, tmp_path: Path) -> None:
        """``ring quiet off`` 呼叫的 force=True：跳過 quiet／視窗判斷，queue 有東西就 flush。"""
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        now = 1000.0
        try_claim_leading_edge(now, 1, queue_path=q)  # 開視窗（seconds 值在此無關緊要，只是設置起點）
        enqueue([_s("a")], queue_path=q)
        set_quiet(None, quiet_path=quiet)
        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(999)),
            patch("ring.notify.notify_summary") as mock_summary,
        ):
            flush_if_due(now=now + 1, force=True, queue_path=q, quiet_path=quiet)
        mock_summary.assert_called_once()
        assert peek_count(queue_path=q) == 0

    def test_flush_failure_is_swallowed(self, tmp_path: Path) -> None:
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        enqueue([_s("a")], queue_path=q)
        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(0)),
            patch("ring.notify.notify_summary", side_effect=Exception("boom")),
        ):
            flush_if_due(now=time.time(), queue_path=q, quiet_path=quiet)  # 不應拋

    def test_concurrent_flush_pops_exactly_once(self, tmp_path: Path) -> None:
        """並發 flush（check-該不該-then-pop-then-清視窗 併進同一鎖區塊）：多個 thread
        同時對一個已過期視窗呼叫 flush_if_due，彙總只能被觸發一次、session 只被 pop 一次
        ——不是像舊版拆成多次 _locked() 那樣可能被重複 pop 或把別人剛開的新視窗清掉。"""
        q, quiet = tmp_path / "queue.json", tmp_path / "quiet"
        now = 1000.0
        try_claim_leading_edge(now, 1, queue_path=q)  # 開視窗（seconds 值在此無關緊要，只是設置起點）
        enqueue([_s(f"s{i}") for i in range(5)], queue_path=q)

        summary_calls: list[int] = []
        lock = threading.Lock()

        def worker() -> None:
            flush_if_due(now=now + 11, queue_path=q, quiet_path=quiet)

        def fake_notify_summary(count: int, sample: object) -> None:
            with lock:
                summary_calls.append(count)

        with (
            patch("ring.notify_queue.get_config", return_value=self._cfg(10)),
            patch("ring.notify.notify_summary", side_effect=fake_notify_summary),
        ):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

        assert summary_calls == [5]  # 只有一個 thread 真的 pop 到、發了一次彙總，其餘拿到空 queue
        assert peek_count(queue_path=q) == 0
