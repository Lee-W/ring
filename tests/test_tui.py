from pathlib import Path
from unittest.mock import patch

import pytest
from textual.widgets import DataTable

import ring.tui as tui
from ring.registry import Session, Status


@pytest.fixture(autouse=True)
def _silence_notifications(monkeypatch: pytest.MonkeyPatch) -> None:
    """TUI 不再自己發系統通知（改由 ring hook 在事件當下發），但 _ring_on_waiting_alerts
    仍會響 in-app 鈴。測試時把 RingApp.bell 擋掉，保持 hermetic、不吵。"""
    monkeypatch.setattr(tui.RingApp, "bell", lambda self: None, raising=False)


@pytest.mark.asyncio
async def test_tui_mounts_and_lists_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [
        Session("a", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "scan"),
        Session("b", "/y/blog", Status.WORKING, 0.0, "hi", "scan"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1, 2])

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        assert table.row_count == 2
        await pilot.press("a")  # toggle 含已離場；這批沒有 ended，列數不變
        assert table.row_count == 2


@pytest.mark.asyncio
async def test_vim_keys_move_row_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [
        Session("a", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "scan"),
        Session("b", "/y/blog", Status.WORKING, 0.0, "hi", "scan"),
        Session("c", "/z/ring", Status.IDLE, 0.0, "hi", "scan"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1, 2, 3])

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        assert table.cursor_row == 0
        await pilot.press("j")  # 下移
        assert table.cursor_row == 1
        await pilot.press("k")  # 上移
        assert table.cursor_row == 0
        await pilot.press("G")  # 跳到最後一列
        assert table.cursor_row == 2
        await pilot.press("g")  # 跳回第一列
        assert table.cursor_row == 0


@pytest.mark.asyncio
async def test_tui_jump_without_tmux_target_does_not_crash(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [Session("a", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "scan")]  # tmux_target=None
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        await pilot.press("enter")  # 沒 tmux 座標 → 只 notify，不該炸
        assert app.query_one(DataTable).row_count == 1


@pytest.mark.asyncio
async def test_new_waiting_rings_bell(monkeypatch: pytest.MonkeyPatch) -> None:
    state: dict[str, list[Session]] = {"sessions": [Session("a", "/x/p", Status.WORKING, 0.0, "-", "scan")]}
    monkeypatch.setattr(tui, "board", lambda show_all: state["sessions"])
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

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
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

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
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

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
    """有有效 request + _own_tty 非空 → 游標移到對應 row、記住 _focused_sid，且 focus_jump 被呼叫餵自己的 tty。"""
    from unittest.mock import MagicMock

    from ring.ipc import write_focus_request

    req_path = tmp_path / "focus-request"
    sessions = [
        Session("first-id", "/x/proj1", Status.WAITING, 0.0, "→ Edit", "hook"),
        Session("second-id", "/y/proj2", Status.WORKING, 0.0, "hi", "hook"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    # 預先寫入 request
    write_focus_request("second-id", request_path=req_path)

    def _read_with_tmp(**kw: object) -> str | None:
        from ring.ipc import read_focus_request as _r

        return _r(request_path=req_path)

    # mock focus_jump（self-activation 複用的 focuser 鏈入口）
    mock_jump = MagicMock(return_value=(True, "mocked-tty"))

    app = tui.RingApp(lang="en")
    # 設定 _own_tty 非空，讓 activate 路徑實際執行
    app._own_tty = "/dev/ttys001"

    with (
        patch("ring.tui.read_focus_request", _read_with_tmp),
        patch("ring.tui.focus_jump", mock_jump),
    ):
        async with app.run_test():
            table = app.query_one(DataTable)
            # _reload 已在 on_mount 呼叫並觸發 _poll_focus_request → cursor 應在 row=1
            assert table.cursor_row == 1
            # request 被消費
            assert not req_path.exists()
            # 通知指向的 session 被記住，供持續標記
            assert app._focused_sid == "second-id"
            # activate 路徑：focus_jump 有被呼叫，且餵的是自己的 tty
            assert mock_jump.called
            assert mock_jump.call_args.args[0].tty == "/dev/ttys001"


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
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    write_focus_request("second-id", request_path=req_path)

    def _read_with_tmp(**kw: object) -> str | None:
        from ring.ipc import read_focus_request as _r

        return _r(request_path=req_path)

    mock_jump = MagicMock()

    app = tui.RingApp(lang="en")
    # headless：_own_tty 為空字串，activate 整段應被跳過
    app._own_tty = ""

    with (
        patch("ring.tui.read_focus_request", _read_with_tmp),
        patch("ring.tui.focus_jump", mock_jump),
    ):
        async with app.run_test():
            table = app.query_one(DataTable)
            # 游標仍正確移到 row=1
            assert table.cursor_row == 1
            # request 被消費
            assert not req_path.exists()
            # activate 路徑被跳過：focus_jump 完全沒被呼叫
            mock_jump.assert_not_called()


@pytest.mark.asyncio
async def test_poll_focus_request_session_not_found_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """request session_id 不在 sessions → 走不在場分支，不崩。"""
    from ring.ipc import write_focus_request

    req_path = tmp_path / "focus-request"
    sessions = [Session("only-id", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    write_focus_request("nonexistent-id", request_path=req_path)

    def _read_with_tmp(**kw: object) -> str | None:
        from ring.ipc import read_focus_request as _r

        return _r(request_path=req_path)

    app = tui.RingApp(lang="en")
    with patch("ring.tui.read_focus_request", _read_with_tmp):
        async with app.run_test():
            # 不崩即可
            assert app.query_one(DataTable).row_count == 1


@pytest.mark.asyncio
async def test_label_shown_in_project_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    """有自訂標籤的 session → 專案欄直接顯示標籤（取代 workspace 名）。"""
    sessions = [Session("sess-1", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "hook")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])
    monkeypatch.setattr(tui, "load_labels", lambda **kw: {"sess-1": "重構登入"})

    app = tui.RingApp(lang="en")
    async with app.run_test():
        table = app.query_one(DataTable)
        cells = [str(c) for c in table.get_row_at(0)]
        project_cell = cells[1]  # 單一 provider → 無工具欄，專案欄在 index 1
        assert project_cell == "重構登入"  # 取了名就取代 workspace 名


@pytest.mark.asyncio
async def test_tui_hides_tool_column_when_uniform(monkeypatch: pytest.MonkeyPatch) -> None:
    """全同一個 provider → 啟動時不加工具欄（6 欄）；混用 → 加（7 欄）。"""
    uniform = [
        Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
        Session("b", "/y/ring", Status.WORKING, 0.0, "-", "hook", provider="claude-code"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: uniform)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])
    app = tui.RingApp(lang="en")
    async with app.run_test():
        assert len(app.query_one(DataTable).columns) == 6  # 無工具欄

    mixed = [
        Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
        Session("b", "/y/ring", Status.WORKING, 0.0, "-", "codex", provider="codex"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: mixed)
    app2 = tui.RingApp(lang="en")
    async with app2.run_test():
        assert len(app2.query_one(DataTable).columns) == 7  # 有工具欄


@pytest.mark.asyncio
async def test_tool_column_disappears_after_reload_to_uniform(monkeypatch: pytest.MonkeyPatch) -> None:
    """刷新後 provider 從混用（7 欄）變單一（6 欄），工具欄動態消失。"""
    state: dict[str, list[Session]] = {
        "sessions": [
            Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
            Session("b", "/y/ring", Status.WORKING, 0.0, "-", "codex", provider="codex"),
        ]
    }
    monkeypatch.setattr(tui, "board", lambda show_all: state["sessions"])
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test():
        table = app.query_one(DataTable)
        assert len(table.columns) == 7  # 混用 → 工具欄存在

        # 模擬刷新後全改成同一個 provider
        state["sessions"] = [
            Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
            Session("b", "/y/ring", Status.WORKING, 0.0, "-", "hook", provider="claude-code"),
        ]
        app._reload()
        assert len(table.columns) == 6  # 工具欄消失


@pytest.mark.asyncio
async def test_tool_column_appears_after_reload_to_mixed(monkeypatch: pytest.MonkeyPatch) -> None:
    """刷新後 provider 從單一（6 欄）變混用（7 欄），工具欄動態出現。"""
    state: dict[str, list[Session]] = {
        "sessions": [
            Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
            Session("b", "/y/ring", Status.WORKING, 0.0, "-", "hook", provider="claude-code"),
        ]
    }
    monkeypatch.setattr(tui, "board", lambda show_all: state["sessions"])
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test():
        table = app.query_one(DataTable)
        assert len(table.columns) == 6  # 全同一種 → 無工具欄

        # 模擬刷新後混入第二種 provider
        state["sessions"] = [
            Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
            Session("b", "/y/ring", Status.WORKING, 0.0, "-", "codex", provider="codex"),
        ]
        app._reload()
        assert len(table.columns) == 7  # 工具欄出現


@pytest.mark.asyncio
async def test_cursor_preserved_across_column_switch(monkeypatch: pytest.MonkeyPatch) -> None:
    """欄位切換（混用→單一）時，游標停在原本選的列，不被 clear(columns=True) reset 成 0。"""
    state: dict[str, list[Session]] = {
        "sessions": [
            Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
            Session("b", "/y/ring", Status.WORKING, 0.0, "-", "codex", provider="codex"),
            Session("c", "/z/app", Status.IDLE, 0.0, "-", "codex", provider="codex"),
        ]
    }
    monkeypatch.setattr(tui, "board", lambda show_all: state["sessions"])
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test():
        table = app.query_one(DataTable)
        table.move_cursor(row=2)  # 使用者選到第 3 列

        # 刷新後全改成同一個 provider → 觸發欄位切換（7→6 欄）
        state["sessions"] = [
            Session("a", "/x/maigo", Status.WAITING, 0.0, "-", "hook", provider="claude-code"),
            Session("b", "/y/ring", Status.WORKING, 0.0, "-", "hook", provider="claude-code"),
            Session("c", "/z/app", Status.IDLE, 0.0, "-", "hook", provider="claude-code"),
        ]
        app._reload()
        assert table.cursor_row == 2  # 游標還在第 3 列，沒被 reset


@pytest.mark.asyncio
async def test_name_session_saves_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """按 n → 輸入 → Enter，會以 (session_id, 輸入值) 呼叫 set_label。"""
    sessions = [Session("sess-1", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "hook")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])
    monkeypatch.setattr(tui, "load_labels", lambda **kw: {})
    monkeypatch.setattr(tui, "get_label", lambda sid, **kw: "")

    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(tui, "set_label", lambda sid, label, **kw: saved.append((sid, label)))

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        await pilot.press("n")  # 開命名浮層
        await pilot.pause()
        for ch in "task1":
            await pilot.press(ch)
        await pilot.press("enter")
        await pilot.pause()

    assert saved == [("sess-1", "task1")]


@pytest.mark.asyncio
async def test_name_session_escape_does_not_save(monkeypatch: pytest.MonkeyPatch) -> None:
    """按 n → Esc 取消，不呼叫 set_label。"""
    sessions = [Session("sess-1", "/x/maigo", Status.WAITING, 0.0, "→ Edit", "hook")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])
    monkeypatch.setattr(tui, "load_labels", lambda **kw: {})
    monkeypatch.setattr(tui, "get_label", lambda sid, **kw: "")

    saved: list[tuple[str, str]] = []
    monkeypatch.setattr(tui, "set_label", lambda sid, label, **kw: saved.append((sid, label)))

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        await pilot.press("n")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()

    assert saved == []


@pytest.mark.asyncio
async def test_delete_session_requires_dd_and_clears_label(monkeypatch: pytest.MonkeyPatch) -> None:
    """按 d 只 arm；第二次 d 才刪 RiNG registry，並清掉該 session label。"""
    state: dict[str, list[Session]] = {
        "sessions": [Session("sess-1", "/x/maigo", Status.ENDED, 0.0, "—", "hook")]
    }
    monkeypatch.setattr(tui, "board", lambda show_all: state["sessions"])
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [])
    monkeypatch.setattr(tui, "load_labels", lambda **kw: {"sess-1": "舊 session"})
    monkeypatch.setattr(tui, "get_label", lambda sid, **kw: "舊 session")

    deleted: list[str] = []
    hidden: list[str] = []
    labels: list[tuple[str, str]] = []

    def _delete(sid: str) -> bool:
        deleted.append(sid)
        state["sessions"] = []
        return True

    monkeypatch.setattr(tui, "delete_session_state", _delete)
    monkeypatch.setattr(tui, "hide_session", lambda sid: hidden.append(sid))
    monkeypatch.setattr(tui, "set_label", lambda sid, label, **kw: labels.append((sid, label)))

    app = tui.RingApp(lang="en", show_all=True)
    async with app.run_test() as pilot:
        table = app.query_one(DataTable)
        assert table.row_count == 1

        await pilot.press("d")
        assert deleted == []
        assert hidden == []
        assert table.row_count == 1

        await pilot.press("d")
        assert deleted == ["sess-1"]
        assert hidden == ["sess-1"]
        assert labels == [("sess-1", "")]
        assert table.row_count == 0


@pytest.mark.asyncio
async def test_delete_session_hides_non_registry_source(monkeypatch: pytest.MonkeyPatch) -> None:
    """scan/Codex 原始來源沒有 RiNG registry 可刪時，仍寫 tombstone 讓列消失。"""
    from textual.widgets import Static

    hidden: set[str] = set()
    sessions = [Session("scan-1", "/x/maigo", Status.IDLE, 0.0, "—", "scan")]
    monkeypatch.setattr(tui, "board", lambda show_all: [s for s in sessions if s.session_id not in hidden])
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [])
    monkeypatch.setattr(tui, "delete_session_state", lambda sid: False)
    monkeypatch.setattr(tui, "hide_session", lambda sid: hidden.add(sid))

    cleared: list[tuple[str, str]] = []
    monkeypatch.setattr(tui, "set_label", lambda sid, label, **kw: cleared.append((sid, label)))

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        await pilot.press("d")
        await pilot.press("d")

        assert app.query_one(DataTable).row_count == 0
        assert cleared == [("scan-1", "")]
        assert "Hidden" in str(app.query_one("#status", Static).render())


@pytest.mark.asyncio
async def test_focused_highlight_clears_when_no_longer_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """通知指向的 session 回應後（離開 WAITING）→ 持續標記自動解除。"""
    from ring.ipc import write_focus_request

    req_path = tmp_path / "focus-request"
    state: dict[str, list[Session]] = {"sessions": [Session("sid-1", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")]}
    monkeypatch.setattr(tui, "board", lambda show_all: state["sessions"])
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    write_focus_request("sid-1", request_path=req_path)

    def _read_once(**kw: object) -> str | None:
        from ring.ipc import read_focus_request as _r

        return _r(request_path=req_path)

    app = tui.RingApp(lang="en")
    app._own_tty = ""  # 跳過 activate
    with patch("ring.tui.read_focus_request", _read_once):
        async with app.run_test():
            assert app._focused_sid == "sid-1"  # 通知指向已記住
            # session 回應 → 不再 WAITING
            state["sessions"] = [Session("sid-1", "/x/proj", Status.WORKING, 0.0, "hi", "hook")]
            app._reload()
            assert app._focused_sid is None  # 標記自動解除


@pytest.mark.asyncio
async def test_detail_row_shows_waiting_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    """選中 🔴 等你且有 waiting_detail 的列 → detail 列顯示它在等什麼。"""
    from textual.widgets import Static

    sessions = [
        Session(
            "w",
            "/x/maigo",
            Status.WAITING,
            0.0,
            "→ 等你",
            "hook",
            waiting_kind="permission",
            waiting_detail="Bash: rm -rf node_modules",
        ),
        Session("b", "/y/blog", Status.WORKING, 0.0, "→ Edit", "hook"),
    ]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        detail = app.query_one("#detail", Static)
        # WAITING 排最上面，游標預設在第 0 列 → 顯示 detail
        assert "🔐" in str(detail.render())
        assert "rm -rf node_modules" in str(detail.render())
        # 移到沒有 detail 的列 → 清空
        app.query_one(DataTable).move_cursor(row=1)
        await pilot.pause()
        assert str(detail.render()) == ""


@pytest.mark.asyncio
async def test_jump_oldest_waiting_hotkey(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [
        Session("newer", "/x/new", Status.WAITING, 200.0, "→ newer", "hook"),
        Session("older", "/x/old", Status.WAITING, 100.0, "→ older", "hook"),
        Session("idle", "/x/idle", Status.IDLE, 50.0, "—", "hook"),
    ]
    jumped: list[str] = []

    def fake_focus_jump(s: Session) -> tuple[bool, str]:
        jumped.append(s.session_id)
        return True, "jumped"

    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])
    monkeypatch.setattr(tui, "focus_jump", fake_focus_jump)

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        await pilot.press("w")
        assert jumped == ["older"]
        assert app.query_one(DataTable).cursor_row == 1


@pytest.mark.asyncio
async def test_jump_oldest_waiting_hotkey_without_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    from textual.widgets import Static

    sessions = [Session("idle", "/x/idle", Status.IDLE, 50.0, "—", "hook")]
    monkeypatch.setattr(tui, "board", lambda show_all: sessions)
    monkeypatch.setattr(tui, "running_agent_pids", lambda: [1])

    app = tui.RingApp(lang="en")
    async with app.run_test() as pilot:
        await pilot.press("w")
        assert "session" in str(app.query_one("#status", Static).render()).lower()
