"""RiNG 的 Textual live TUI——鍵盤導覽 + tmux / iTerm2 一鍵跳。

需要 textual（``pip install 'ring[tui]'``）。沒裝時 CLI 會自動退回 Rich poll。

鍵：↑/↓ 選 session、Enter/Space 跳到它所在的終端、a 切換是否顯示已離場、r 刷新、q 離場。
"""

from __future__ import annotations

import os
from typing import ClassVar

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.widgets import DataTable, Footer, Header, Static

from ring.cli import _LOC_MAX, _STATUS_STYLE, _header, _middle_truncate, _rel, board, status_label
from ring.config import get_config
from ring.focus import jump as focus_jump
from ring.i18n import gettext as _
from ring.i18n import set_lang
from ring.ipc import clear_tui_presence, read_focus_request, write_tui_presence
from ring.notify import notify_waiting
from ring.registry import Session, Status, running_agent_pids
from ring.watcher import WaitingAlertScheduler

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
        cfg = get_config()
        self._alerts = WaitingAlertScheduler(cfg.notify_repeat_seconds, cfg.notify_repeat_max)
        self.title = "RiNG 🎤"
        # 記下自己的 controlling tty，供 _poll_focus_request activate 視窗用。
        self._own_tty: str = self._detect_own_tty()

    @staticmethod
    def _detect_own_tty() -> str:
        """取得 controlling terminal 的 tty 路徑，取不到就回空字串。"""
        import sys as _sys

        try:
            if _sys.stdout.isatty():
                return os.ttyname(_sys.stdout.fileno())
        except Exception:
            pass
        try:
            fd = os.open("/dev/tty", os.O_RDONLY | os.O_NOCTTY)
            try:
                return os.ttyname(fd)
            finally:
                os.close(fd)
        except Exception:
            return ""

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Static(id="legend")
        yield DataTable(id="grid", zebra_stripes=True)
        yield Static(id="status")
        yield Footer()

    def on_mount(self) -> None:
        # 寫入 presence，讓 `ring focus` 知道 TUI 在跑。
        write_tui_presence()
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

    def on_unmount(self) -> None:
        # 清除 presence，TUI 離場後 `ring focus` 退回 headless 行為。
        clear_tui_presence()

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

    def _ring_on_waiting_alerts(self, alerts: list[Session]) -> None:
        """有 session 需要提醒 → RiNG 真的「ring」你（響鈴 + toast 通知）。"""
        if alerts:
            self.bell()
            names = ", ".join(sorted(s.project for s in alerts))
            self.notify(_("🔔 {names} 在等你回話", names=names), timeout=8)

    def _poll_focus_request(self) -> None:
        """讀一次 focus-request 檔，有效的話把游標跳過去並 activate 自己視窗。

        流程（對應 plan 設計 C.1–C.7）：
        1. 讀 focus-request；無 / 過期 / 解析失敗 → 直接回傳（read_focus_request 已消費即焚）。
        2. 在 _sessions 找 session_id → 取得 row index。
        3. 移游標到對應 row。
        4. 用 AppleScriptTTYFocuser activate 自己視窗（reuse 既有機制，餵自己的 tty）。
        5. 設 status 提示 + notify。
        6. 找不到 session_id → 走「已不在場」分支，仍 activate 視窗讓使用者看到全貌。
        """
        from ring.focus.base import AppleScriptTTYFocuser
        from ring.focus.iterm2 import _SCRIPT as _iterm2_script
        from ring.focus.terminal import _SCRIPT as _terminal_script

        sid = read_focus_request()
        if sid is None:
            return

        # 找到 session → 取得 row index
        target_row: int | None = None
        for idx, s in enumerate(self._sessions):
            if s.session_id == sid:
                target_row = idx
                break

        # activate 自己視窗（best-effort，失敗安靜吞）
        tty = self._own_tty
        if tty:
            try:
                _dummy = Session(sid, "/", Status.WORKING, 0.0, "", "ipc", tty=tty)
                for script in (_iterm2_script, _terminal_script):
                    focuser = AppleScriptTTYFocuser("self", script)
                    result = focuser.try_focus(_dummy)
                    if result is not None and result[0]:
                        break
            except Exception:
                pass

        if target_row is not None:
            table = self.query_one(DataTable)
            table.move_cursor(row=target_row)
            found_session = self._sessions[target_row]
            msg = _("→ 已跳到 {project}（來自通知）", project=found_session.project)
            self._set_status(msg)
            self.notify(msg, timeout=8)
        else:
            msg = _("那個 session 已不在場")
            self._set_status(msg)
            self.notify(msg, severity="warning", timeout=8)

    def _reload(self) -> None:
        self._sessions = board(self._show_all)
        alerts = self._alerts.feed(self._sessions)
        self._ring_on_waiting_alerts(alerts)
        try:
            hint = notify_waiting(alerts)
            if hint:
                self.notify(hint, timeout=10)
        except Exception:
            pass
        self.sub_title = _header(len(self._sessions), len(running_agent_pids()))
        table = self.query_one(DataTable)
        cursor = table.cursor_row
        table.clear()
        for s in self._sessions:
            status_cell = Text(f"{s.status.marker} {status_label(s.status)}", style=_STATUS_STYLE[s.status])
            progress = f"{s.todo[0]}/{s.todo[1]}" if s.todo else "·"
            loc_cell = f"📍{_middle_truncate(s.location, _LOC_MAX)}"
            table.add_row(status_cell, s.project, progress, _rel(s.idle_for), loc_cell, s.last_action)
        if self._sessions:
            table.move_cursor(row=min(cursor, len(self._sessions) - 1))
        self._poll_focus_request()

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
