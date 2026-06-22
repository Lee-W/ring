"""IPC helper 單元測試——write/read round-trip、過期、消費即焚、pid stale、tmp 路徑注入。"""

from __future__ import annotations

import os
import time
from pathlib import Path

from ring.ipc import (
    clear_tui_presence,
    read_focus_request,
    read_tui_presence,
    write_focus_request,
    write_tui_presence,
)

# --------------------------------------------------------------------------- focus-request


class TestFocusRequest:
    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        """write → read 拿回 session_id。"""
        req_path = tmp_path / "focus-request"
        write_focus_request("abc-123", request_path=req_path)
        result = read_focus_request(request_path=req_path)
        assert result == "abc-123"

    def test_consumed_after_read(self, tmp_path: Path) -> None:
        """讀取後檔案被刪（消費即焚）。"""
        req_path = tmp_path / "focus-request"
        write_focus_request("abc-123", request_path=req_path)
        read_focus_request(request_path=req_path)
        assert not req_path.exists()

    def test_expired_request_returns_none(self, tmp_path: Path) -> None:
        """過期 request 被忽略，回傳 None。"""
        req_path = tmp_path / "focus-request"
        write_focus_request("abc-123", request_path=req_path)
        # 讀取時傳 ttl=0 讓任何 ts 都過期
        result = read_focus_request(request_path=req_path, ttl=0.0)
        assert result is None

    def test_expired_request_file_deleted(self, tmp_path: Path) -> None:
        """過期的 request 檔仍被刪掉（不殘留）。"""
        req_path = tmp_path / "focus-request"
        write_focus_request("abc-123", request_path=req_path)
        read_focus_request(request_path=req_path, ttl=0.0)
        assert not req_path.exists()

    def test_no_file_returns_none(self, tmp_path: Path) -> None:
        """無 request 檔 → None。"""
        req_path = tmp_path / "focus-request"
        assert read_focus_request(request_path=req_path) is None

    def test_corrupt_json_returns_none_and_deletes(self, tmp_path: Path) -> None:
        """爛 JSON → None，並刪掉爛檔。"""
        req_path = tmp_path / "focus-request"
        req_path.write_text("not json", encoding="utf-8")
        result = read_focus_request(request_path=req_path)
        assert result is None
        assert not req_path.exists()

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        """write 時自動建立父目錄。"""
        req_path = tmp_path / "nested" / "dir" / "focus-request"
        write_focus_request("abc", request_path=req_path)
        assert req_path.exists()

    def test_second_read_returns_none(self, tmp_path: Path) -> None:
        """第二次讀取（檔已不存在）→ None。"""
        req_path = tmp_path / "focus-request"
        write_focus_request("abc-123", request_path=req_path)
        read_focus_request(request_path=req_path)
        assert read_focus_request(request_path=req_path) is None


# --------------------------------------------------------------------------- tui-presence


class TestTuiPresence:
    def test_write_and_read_round_trip(self, tmp_path: Path) -> None:
        """write → read 拿回含 tty/pid/ts 的 presence。"""
        pres_path = tmp_path / "tui-presence"
        write_tui_presence(presence_path=pres_path)
        result = read_tui_presence(presence_path=pres_path)
        assert result is not None
        assert result["pid"] == os.getpid()
        assert isinstance(result["ts"], float)

    def test_no_file_returns_none(self, tmp_path: Path) -> None:
        """無 presence 檔 → None。"""
        pres_path = tmp_path / "tui-presence"
        assert read_tui_presence(presence_path=pres_path) is None

    def test_expired_presence_returns_none(self, tmp_path: Path) -> None:
        """過期 presence → None，且刪檔。"""
        pres_path = tmp_path / "tui-presence"
        write_tui_presence(presence_path=pres_path)
        result = read_tui_presence(presence_path=pres_path, ttl=0.0)
        assert result is None
        assert not pres_path.exists()

    def test_dead_pid_returns_none(self, tmp_path: Path) -> None:
        """pid 已死 → None，且刪檔。"""
        import json

        pres_path = tmp_path / "tui-presence"
        # 寫一個不存在 pid 的 presence（PID 999999 極不可能存在）
        pres_path.write_text(
            json.dumps({"tty": "/dev/ttys000", "pid": 999999, "ts": time.time()}),
            encoding="utf-8",
        )
        result = read_tui_presence(presence_path=pres_path)
        assert result is None
        assert not pres_path.exists()

    def test_clear_deletes_file(self, tmp_path: Path) -> None:
        """clear_tui_presence 刪掉 presence 檔。"""
        pres_path = tmp_path / "tui-presence"
        write_tui_presence(presence_path=pres_path)
        assert pres_path.exists()
        clear_tui_presence(presence_path=pres_path)
        assert not pres_path.exists()

    def test_clear_nonexistent_does_not_raise(self, tmp_path: Path) -> None:
        """clear 不存在的檔案不拋例外。"""
        pres_path = tmp_path / "tui-presence"
        clear_tui_presence(presence_path=pres_path)  # 不應拋

    def test_current_pid_is_alive(self, tmp_path: Path) -> None:
        """當前 process 的 pid 一定活著，read 應回傳 presence。"""
        pres_path = tmp_path / "tui-presence"
        write_tui_presence(presence_path=pres_path)
        result = read_tui_presence(presence_path=pres_path)
        assert result is not None

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        """write 時自動建立父目錄。"""
        pres_path = tmp_path / "nested" / "tui-presence"
        write_tui_presence(presence_path=pres_path)
        assert pres_path.exists()
