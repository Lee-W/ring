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
