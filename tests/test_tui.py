from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import DataTable

import ring.tui as tui
from ring.registry import Session, Status


@pytest.mark.asyncio
async def test_tui_mounts_and_lists_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [
        Session("a", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "scan"),
        Session("b", "/y/blog", Status.WORKING, 0.0, "hi", "scan"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1, 2])

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        assert table.row_count == 2
        await pilot.press("a")  # toggle 含已離場；這批沒有 ended，列數不變
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_tui_jump_without_tmux_target_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [Session("a", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "scan")]  # tmux_target=None
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        await pilot.press("enter")  # 沒 tmux 座標 → 只 notify，不該炸
        assert app.query_one(DataTable).row_count == 1


@pytest.mark.asyncio
async def test_new_waiting_rings_bell(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, list[Session]] = {"sessions": [Session("a", "/x/p", Status.WORKING, 0.0, "-", "scan")]}
    monkeypatch.setattr(tui, "board", lambda show_all: state["sessions"])
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test():
        bells: list[int] = []
        monkeypatch.setattr(app, "bell", lambda: bells.append(1))
        state["sessions"] = [Session("a", "/x/p", Status.WAITING, 0.0, "-", "scan")]  # WORKING → WAITING
        app._reload()
        assert bells == [1]


# ---------------------------------------------------------------------------
# presence 生命週期
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tui_writes_presence_on_mount(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TUI 啟動後 presence 檔存在。"""
    sessions = [Session("a", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "scan")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1])

    pres_path = tmp_path / "tui-presence"

    with patch("ring.tui.write_tui_presence", lambda **kw: pres_path.touch()):
        app = tui.RingApp(lang="en")
        async with app.run_test():
            pass  # on_mount 已呼叫 write_tui_presence

    # write_tui_presence 被呼叫（用 touch 模擬）
    assert pres_path.exists()


@pytest.mark.asyncio
async def test_tui_clears_presence_on_unmount(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """TUI 離場後 clear_tui_presence 被呼叫。"""
    sessions = [Session("a", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "scan")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1])

    cleared: list[int] = []

    with patch("ring.tui.clear_tui_presence", lambda **kw: cleared.append(1)):
        app = tui.RingApp(lang="en")
        async with app.run_test():
            pass  # on_unmount 呼叫 clear_tui_presence

    assert cleared == [1]


# ---------------------------------------------------------------------------
# _poll_focus_request
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_poll_focus_request_moves_cursor(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """有有效 request + _own_tty 非空 → 游標移到對應 row，且 AppleScriptTTYFocuser.try_focus 被呼叫。"""
    from unittest.mock import MagicMock

    from ring.ipc import write_focus_request

    req_path = tmp_path / "focus-request"
    sessions = [
        Session("first-id", "/x/proj1", Status.WAITING, 0.0, "→ Edit", "hook"),
        Session("second-id", "/y/proj2", Status.WORKING, 0.0, "hi", "hook"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1])

    # 預先寫入 request
    write_focus_request("second-id", request_path=req_path)

    def _read_with_tmp(**kw: object) -> str | None:
        from ring.ipc import read_focus_request as _r

        return _r(request_path=req_path)

    # mock AppleScriptTTYFocuser：讓 try_focus 回傳成功，並記錄呼叫
    mock_focuser_instance = MagicMock()
    mock_focuser_instance.try_focus.return_value = (True, "mocked-tty")
    mock_focuser_cls = MagicMock(return_value=mock_focuser_instance)

    app = tui.RingApp(lang="en")
    # 設定 _own_tty 非空，讓 activate 路徑實際執行
    app._own_tty = "/dev/ttys001"

    with (
        patch("ring.tui.read_focus_request", _read_with_tmp),
        patch("ring.focus.base.AppleScriptTTYFocuser", mock_focuser_cls),
    ):
        async with app.run_test():
            table = app.query_one(DataTable)
            # _reload 已在 on_mount 呼叫並觸發 _poll_focus_request → cursor 應在 row=1
            assert table.cursor_row == 1
            # request 被消費
            assert not req_path.exists()
            # activate 路徑：AppleScriptTTYFocuser 有被實例化並呼叫 try_focus
            assert mock_focuser_cls.called
            assert mock_focuser_instance.try_focus.called


@pytest.mark.asyncio
async def test_poll_focus_request_headless_skips_activate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """_own_tty = ""（headless）→ activate 整段被跳過、不崩、游標仍正確移動。"""
    from unittest.mock import MagicMock

    from ring.ipc import write_focus_request

    req_path = tmp_path / "focus-request"
    sessions = [
        Session("first-id", "/x/proj1", Status.WAITING, 0.0, "→ Edit", "hook"),
        Session("second-id", "/y/proj2", Status.WORKING, 0.0, "hi", "hook"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1])

    write_focus_request("second-id", request_path=req_path)

    def _read_with_tmp(**kw: object) -> str | None:
        from ring.ipc import read_focus_request as _r

        return _r(request_path=req_path)

    mock_focuser_cls = MagicMock()

    app = tui.RingApp(lang="en")
    # headless：_own_tty 為空字串，activate 整段應被跳過
    app._own_tty = ""

    with (
        patch("ring.tui.read_focus_request", _read_with_tmp),
        patch("ring.focus.base.AppleScriptTTYFocuser", mock_focuser_cls),
    ):
        async with app.run_test():
            table = app.query_one(DataTable)
            # 游標仍正確移到 row=1
            assert table.cursor_row == 1
            # request 被消費
            assert not req_path.exists()
            # activate 路徑被跳過：AppleScriptTTYFocuser 完全沒被實例化
            mock_focuser_cls.assert_not_called()


@pytest.mark.asyncio
async def test_poll_focus_request_session_not_found_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """request session_id 不在 sessions → 走不在場分支，不崩。"""
    from ring.ipc import write_focus_request

    req_path = tmp_path / "focus-request"
    sessions = [Session("only-id", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_claude_pids", lambda: [1])

    write_focus_request("nonexistent-id", request_path=req_path)

    def _read_with_tmp(**kw: object) -> str | None:
        from ring.ipc import read_focus_request as _r

        return _r(request_path=req_path)

    app = tui.RingApp(lang="en")
    with patch("ring.tui.read_focus_request", _read_with_tmp):
        async with app.run_test():
            # 不崩即可
            assert app.query_one(DataTable).row_count == 1
