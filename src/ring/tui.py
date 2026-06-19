"""RiNG 的 Textual live TUI——鍵盤導覽 + tmux / iTerm2 一鍵跳。

需要 textual（``pip install 'ring-cc[tui]'``）。沒裝時 CLI 會自動退回 Rich poll。

鍵：↑/↓ 選 session、Enter/Space 跳到它所在的終端、a 切換是否顯示已離場、r 刷新、q 離場。
"""

from __future__ import annotations

from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Footer, Header, Static

from ring.cli import _STATUS_STYLE, _header, _rel, board, status_label
from ring.focus import jump as focus_jump
from ring.i18n import gettext as _
from ring.i18n import set_lang
from ring.registry import Session, Status, running_claude_pids

_ORDER = (Status.WAITING, Status.WORKING, Status.IDLE, Status.ENDED)


class RingApp(App[None]):
    """場館的即時看板。"""

    # 按鍵說明在 import 時定下——cli 會在 import 本模組前先 set_lang()，所以吃得到 --lang。
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", _("離場")),
        Binding("r", "refresh_now", _("刷新")),
        Binding("a", "toggle_all", _("含已離場")),
        Binding("enter", "jump", _("跳過去")),
        Binding("space", "jump", _("跳過去"), show=False),
    ]

    def __init__(self, lang: str | None = None, interval: float = 2.0, show_all: bool = False) -> None:
        super().__init__()
        if lang is not None:
            set_lang(lang)
        self._interval = interval
        self._show_all = show_all
        self._sessions: list[Session] = []
        self._waiting_ids: set[str] = set()  # 已知在等你的 session，用來偵測「新轉為等你」
        self._primed = False  # 第一次 reload 只記錄、不響鈴（避免開場就為既有 waiting 響）
        self.title = "RiNG 🎤"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="legend")
        yield DataTable(id="grid", zebra_stripes=True)
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        for label in (_("狀態"), _("專案"), _("進度"), _("閒置"), _("去哪"), _("動作")):
            table.add_column(label)
        table.cursor_type = "row"
        table.focus()  # 讓 ↑/↓ 與 Enter 直接作用在表格上
        self._render_legend()
        self._reload()
        self.set_interval(self._interval, self._reload)
        if not self._hooks_active() and self._has_cwd_collision():
            self._set_status(_("💡 同專案開了多個 session，裝 hook 跳轉才精準：ring install-hooks"))
        else:
            self._set_status(_("↑/↓ 選一列，Enter 或 Space 跳到那個 session 的終端"))

    def _set_status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

    def _hooks_active(self) -> bool:
        return any(s.source == "hook" for s in self._sessions)

    def _has_cwd_collision(self) -> bool:
        """同一個 cwd 有多個還在場的 session → scan 模式分不出 tty，hook 才精準。"""
        live = [s.cwd for s in self._sessions if s.status is not Status.ENDED]
        return len(live) != len(set(live))

    def _render_legend(self) -> None:
        legend = Text(f"{_('圖例')}   ", style="grey50")
        for status in _ORDER:
            legend.append(f"{status.marker} {status_label(status)}   ", style=_STATUS_STYLE[status])
        self.query_one("#legend", Static).update(legend)

    def _ring_on_new_waiting(self) -> None:
        """有 session 新轉為 🔴 等你 → RiNG 真的「ring」你（響鈴 + 通知）。"""
        waiting = {s.session_id for s in self._sessions if s.status is Status.WAITING}
        if self._primed:
            newly = waiting - self._waiting_ids
            if newly:
                self.bell()
                names = ", ".join(sorted(s.project for s in self._sessions if s.session_id in newly))
                self.notify(_("🔔 {names} 在等你回話", names=names), timeout=8)
        self._waiting_ids = waiting
        self._primed = True

    def _reload(self) -> None:
        self._sessions = board(self._show_all)
        self._ring_on_new_waiting()
        self.sub_title = _header(len(self._sessions), len(running_claude_pids()))
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        for s in self._sessions:
            status_cell = Text(f"{s.status.marker} {status_label(s.status)}", style=_STATUS_STYLE[s.status])
            progress = f"{s.todo[0]}/{s.todo[1]}" if s.todo else "·"
            table.add_row(status_cell, s.project, progress, _rel(s.idle_for), f"📍{s.location}", s.last_action)
        if self._sessions:
            table.move_cursor(row=min(cursor, len(self._sessions) - 1))

    def _selected(self) -> Session | None:
        row = self.query_one(DataTable).cursor_row
        if 0 <= row < len(self._sessions):
            return self._sessions[row]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable 被 focus 時會吃掉 Enter（變成 RowSelected），在這裡接、轉成跳轉。
        self.action_jump()

    def action_refresh_now(self) -> None:
        self._reload()

    def action_toggle_all(self) -> None:
        self._show_all = not self._show_all
        self._reload()

    def action_jump(self) -> None:
        s = self._selected()
        if s is None:
            self._set_status(_("（沒有選到 session）"))
            return
        ok, msg = focus_jump(s)
        if ok:
            text = _("→ {project}（{where}）", project=s.project, where=msg)
        else:
            text = _("{project}：{msg}", project=s.project, msg=msg)
        self._set_status(text)
        self.notify(text, severity="information" if ok else "warning", timeout=10)


def run_tui(interval: float = 2.0, show_all: bool = False) -> int:
    RingApp(interval=interval, show_all=show_all).run()
    return 0
