"""RiNG 的 Textual live TUI——鍵盤導覽 + tmux / iTerm2 一鍵跳。

需要 textual（``pip install 'ring[tui]'``）。沒裝時 CLI 會自動退回 Rich poll。

鍵：↑/↓（或 vim 的 j/k、g/G 跳頭尾）選 session、Enter/Space 跳到它所在的終端、
p 就地回覆權限請求（tmux 內的 session）、a 切換是否顯示已離場、dd 隱藏 session
（有新活動會自動重新出現）、r 刷新、q 離場。
"""

from __future__ import annotations

import os
import time
from typing import ClassVar

from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static

from ring import permission
from ring.cli import (
    _LOC_MAX,
    _STATUS_STYLE,
    _header,
    _middle_truncate,
    _rel,
    board,
    labeled_project,
    provider_label,
    show_tool_column,
    status_label,
)
from ring.config import get_config
from ring.focus import jump as focus_jump
from ring.i18n import gettext as _
from ring.i18n import set_lang
from ring.ipc import clear_tui_presence, read_focus_request, write_tui_presence
from ring.labels import get_label, load_labels, set_label
from ring.notify_queue import flush_if_due, format_remaining, peek_count, quiet_active, quiet_remaining
from ring.registry import Session, Status, delete_session_state, hide_session, running_agent_pids
from ring.watcher import WaitingAlertScheduler

_ORDER = (Status.WAITING, Status.WORKING, Status.IDLE, Status.ENDED)


def _agent_resume_hint(session_id: str) -> str:
    """背景 agent（無終端可跳）的誠實提示：講清楚沒有畫面可跳，附接回指令。"""
    return _("背景 agent，沒有終端可跳；用 `claude --resume {session_id}` 接回", session_id=session_id)


class _Grid(DataTable[Text]):
    """看板表格。在 DataTable 既有的方向鍵之外，加上 vim 風的 j/k/g/G 導覽。

    全部 ``show=False``——footer 保持乾淨，這些只是給手習慣 vim 的人的隱藏快捷。
    對應 DataTable 既有 action：j/k=cursor_down/up、g/G=scroll_top/bottom（cursor_type
    為 row 時，這兩個會把游標移到第一／最後一列，正是 vim 的語意）。
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("j", "cursor_down", show=False),
        Binding("k", "cursor_up", show=False),
        Binding("g", "scroll_top", show=False),
        Binding("G", "scroll_bottom", show=False),
    ]


class _NameModal(ModalScreen[str | None]):
    """為選中的 session 命名的小浮層。Enter 存、Esc 取消、清空移除。

    dismiss 回傳：輸入字串（含空字串＝清除標籤）或 ``None``（取消，不動）。
    """

    DEFAULT_CSS = """
    _NameModal {
        align: center middle;
    }
    _NameModal #name-box {
        width: 60;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $accent;
    }
    """

    def __init__(self, project: str, current: str) -> None:
        super().__init__()
        self._project = project
        self._current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="name-box"):
            yield Static(_("為 {project} 命名（Enter 存、Esc 取消、清空移除）", project=self._project))
            yield Input(value=self._current, placeholder=_("這個 session 在做什麼…"))

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def key_escape(self) -> None:
        self.dismiss(None)


class _PermissionModal(ModalScreen[int | None]):
    """就地回覆權限請求的浮層：列出對話框原文選項，↑/↓＋Enter 或直接按數字鍵選、Esc 取消。

    dismiss 回傳：選項編號（int）或 ``None``（取消，不送任何鍵）。
    """

    DEFAULT_CSS = """
    _PermissionModal {
        align: center middle;
    }
    _PermissionModal #perm-box {
        width: 90;
        max-width: 100%;
        height: auto;
        padding: 1 2;
        background: $panel;
        border: round $accent;
    }
    _PermissionModal OptionList {
        height: auto;
        background: $panel;
        border: none;
    }
    """

    def __init__(self, project: str, dialog: permission.PermissionDialog) -> None:
        super().__init__()
        self._project = project
        self._dialog = dialog

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-box"):
            yield Static(Text(_("回覆 {project} 的權限請求", project=self._project), style="bold"))
            context = " · ".join(part for part in (self._dialog.title, self._dialog.question) if part)
            if context:
                yield Static(Text(context, style="grey50"))
            yield OptionList(*(f"{n}. {text}" for n, text in self._dialog.options))
            yield Static(Text(_("Enter 或數字鍵送出、Esc 取消"), style="grey50"))

    def on_mount(self) -> None:
        self.query_one(OptionList).focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(self._dialog.options[event.option_index][0])

    def on_key(self, event: events.Key) -> None:
        # 跟真的對話框一樣，直接按選項數字也行。
        if event.key.isdigit() and any(int(event.key) == n for n, _text in self._dialog.options):
            event.stop()
            self.dismiss(int(event.key))

    def key_escape(self) -> None:
        self.dismiss(None)


class RingApp(App[None]):
    """場館的即時看板。"""

    # 按鍵說明在 import 時定下——cli 會在 import 本模組前先 set_lang()，所以吃得到 --lang。
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("q", "quit", _("離場")),
        Binding("r", "refresh_now", _("刷新")),
        Binding("a", "toggle_all", _("含已離場")),
        Binding("n", "name_session", _("命名")),
        Binding("d", "delete_session", _("隱藏"), key_display="dd"),
        Binding("w", "jump_oldest_waiting", _("最久等待")),
        Binding("p", "permission_reply", _("回覆權限")),
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
        # 工具欄是否顯示（啟動時依首批 session 決定：全同一個 provider 就省掉）。
        self._show_tool: bool = True
        # 通知點過來時指向的 session：那一列要持續醒目標記，直到它離開 WAITING（你回應了）或不在場。
        self._focused_sid: str | None = None
        cfg = get_config()
        self._alerts = WaitingAlertScheduler(
            cfg.notify_repeat_seconds, cfg.notify_repeat_max, cooldown_seconds=cfg.waiting_cooldown_seconds
        )
        self.title = "RiNG 🎤"
        # 記下自己的 controlling tty，供 _poll_focus_request activate 視窗用。
        self._own_tty: str = self._detect_own_tty()
        self._delete_armed_sid: str | None = None
        self._delete_armed_until: float = 0.0
        # p 已確認終端對話框消失，但 provider 尚未送後續 hook 時，別讓同一筆舊
        # PermissionRequest 又把列拉回 WAITING。value 是已回覆那筆 row 的 last_active revision。
        self._permission_acks: dict[str, float] = {}

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
        yield _Grid(id="grid", zebra_stripes=True)
        yield Static(id="detail")
        yield Static(id="status")
        yield Footer()

    def _setup_columns(self) -> None:
        """依 self._show_tool 重建 DataTable 欄位。

        呼叫前須先完成 clear(columns=True)（或首次 mount 還沒有欄位）。
        """
        table = self.query_one(DataTable)
        cols = [_("狀態")]
        if self._show_tool:
            cols.append(_("工具"))
        cols += [_("專案"), _("進度"), _("閒置"), _("去哪"), _("動作")]
        for label in cols:
            table.add_column(label)

    def on_mount(self) -> None:
        # 寫入 presence，讓 `ring focus` 知道 TUI 在跑。
        write_tui_presence()
        # 啟動時依首批 session 決定要不要顯示工具欄。
        self._sessions = board(self._show_all)
        self._show_tool = show_tool_column(self._sessions)
        table = self.query_one(DataTable)
        self._setup_columns()
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

    def _clear_delete_armed(self) -> None:
        self._delete_armed_sid = None
        self._delete_armed_until = 0.0

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

    def _display_name(self, s: Session) -> str:
        """訊息 / 提醒用顯示名：取過名用名字，否則用專案名（與看板專案欄一致）。"""
        return labeled_project(s.project, get_label(s.session_id))

    def _ring_on_waiting_alerts(self, alerts: list[Session]) -> None:
        """有 session 需要提醒 → RiNG 真的「ring」你（響鈴 + toast 通知）。

        系統通知原則上由 ``ring hook`` 在轉 🔴 的事件當下發（這裡不重複發，只響
        in-app 鈴）。唯一的例外是 codex 的核可等待：它是讀取側的靜默逾時判定
        （registry._promote_codex_permission_wait），沒有對應 hook 事件可發通知，
        所以由這裡代發系統通知——初次與重複提醒都走 WaitingAlertScheduler 同一條路。
        codex + waiting_kind="permission" 只可能來自逾時判定（hook 對 codex 裸
        PermissionRequest 一律記 working；AskUserQuestion 形狀的是 "question"），
        不會跟 hook 端的通知重複。失敗安靜吞，不影響看板。

        ``notify_waiting(promoted)`` 一定要先呼叫，quiet 判斷放到它後面：
        ``WaitingAlertScheduler.feed()`` 呼叫當下就已經把這批 session 記成「這輪已提醒
        過」（不管呼叫端事後有沒有真的送出提醒），若在呼叫 ``notify_waiting`` 之前就
        因為 quiet active 整段 return，這個 codex 核可等待就會被永久漏掉——它不會被
        ``enqueue()``，quiet 解除後的彙總 flush 也就永遠等不到它，而不是單純延後
        （scheduler 要等 repeat 排程才會再排 due，這段期間內若請求被別的方式解決，
        提醒就徹底消失，沒有任何補發路徑）。``notify_waiting`` 自己的 quiet gate
        會在 quiet active 時把它 enqueue、不發送——這才是 quiet 期間該有的「延後」行為。
        quiet active 時只跳過 in-app 鈴／toast（bell / self.notify），系統通知交給
        ``notify_waiting`` 自己的 gate 決定。
        """
        if not alerts:
            return
        promoted = [s for s in alerts if s.provider == "codex" and s.waiting_kind == "permission"]
        if promoted:
            try:
                from ring.notify import notify_waiting

                notify_waiting(promoted)
            except Exception:
                pass
        if quiet_active(time.time()):
            return
        self.bell()
        names = ", ".join(sorted(self._display_name(s) for s in alerts))
        self.notify(_("🔔 {names} 在等你回話", names=names), timeout=8)

    def _activate_own_window(self) -> None:
        """把 RiNG 自己的終端視窗帶到前景（best-effort，失敗安靜吞）。

        複用既有 focuser 鏈（tmux / iTerm2 / Terminal）：用自己的 tty 組一個 self-Session
        丟給 ``focus_jump``，誰接得住誰來——這樣 tmux 與各 macOS 終端都走同一條路，
        不必在這裡手寫 AppleScript。
        """
        if not self._own_tty:
            return
        try:
            focus_jump(Session("self", "/", Status.WORKING, 0.0, "", "ipc", tty=self._own_tty))
        except Exception:
            pass

    def _poll_focus_request(self) -> None:
        """讀一次 focus-request 檔，有效的話把游標跳過去、持續標記，並 activate 自己視窗。

        1. 讀 focus-request；無 / 過期 / 解析失敗 → 直接回傳（read_focus_request 已消費即焚）。
        2. activate 自己視窗（複用 focuser 鏈）。
        3. 在 _sessions 找 session_id → 移游標 + 記住 _focused_sid（那列持續標記直到回應）。
        4. 找不到 → 走「已不在場」分支，清掉 _focused_sid。
        """
        sid = read_focus_request()
        if sid is None:
            return

        self._activate_own_window()

        target_row: int | None = None
        for idx, s in enumerate(self._sessions):
            if s.session_id == sid:
                target_row = idx
                break

        if target_row is not None:
            self._focused_sid = sid
            table = self.query_one(DataTable)
            table.move_cursor(row=target_row)
            found_session = self._sessions[target_row]
            msg = _("→ 已跳到 {project}（來自通知）", project=self._display_name(found_session))
            self._set_status(msg)
            self.notify(msg, timeout=8)
        else:
            self._focused_sid = None
            msg = _("那個 session 已不在場")
            self._set_status(msg)
            self.notify(msg, severity="warning", timeout=8)

    def _quiet_badge(self) -> str:
        """quiet 狀態 ＋ debounce queue 計數的 header badge；都沒有時回空字串（零視覺負擔）。"""
        now = time.time()
        parts: list[str] = []
        if quiet_active(now):
            remaining = quiet_remaining(now)
            if remaining is None:
                parts.append(_("🔇 QUIET"))
            else:
                parts.append(_("🔇 QUIET · 剩 {remaining}", remaining=format_remaining(remaining)))
        count = peek_count()
        if count > 0:
            parts.append(_("queue: {n}", n=count))
        return "  ｜  " + "  ".join(parts) if parts else ""

    def _reload(self) -> None:
        # 每次刷新都續寫 presence，避免 TUI 開超過 TTL 後 `ring focus` 誤判 TUI 沒在跑、
        # 退回去跳 session 自己的終端（scan 模式常沒 tty → 跳轉失敗）。
        write_tui_presence()
        # TUI 輪詢是三個懶惰 flush 觸發源之一：開著看板時比 headless（靠下個 hook 事件）更快
        # 補發合流的彙總通知。失敗安靜吞，不影響看板本身。
        try:
            flush_if_due()
        except Exception:
            pass
        self._sessions = board(self._show_all)
        self._apply_permission_acks()
        table = self.query_one(DataTable)
        # cursor 快照要在「任何」clear 之前取：clear(columns=True) 會把 cursor_row reset 成 0。
        cursor = table.cursor_row
        # 動態更新工具欄：名單變了（混用 ↔ 全同一種），重建欄位；沒變就省掉欄位重建。
        new_show_tool = show_tool_column(self._sessions)
        if new_show_tool != self._show_tool:
            self._show_tool = new_show_tool
            table.clear(columns=True)  # columns=True 同時清欄位與資料列
            self._setup_columns()
        # 通知指向的 session 一旦離開 WAITING（你回應了）或不在場，就解除醒目標記。
        if self._focused_sid is not None:
            cur = next((s for s in self._sessions if s.session_id == self._focused_sid), None)
            if cur is None or cur.status is not Status.WAITING:
                self._focused_sid = None
        # 系統通知（toast）改由 ``ring hook`` 在事件當下發出（見 hook._ring_waiting_now）；
        # 這裡只留 TUI 自己的 in-app 響鈴 / 訊息列與醒目標記，不重複發系統通知。
        alerts = self._alerts.feed(self._sessions)
        self._ring_on_waiting_alerts(alerts)
        self.sub_title = _header(len(self._sessions), len(running_agent_pids())) + self._quiet_badge()
        labels = load_labels()
        table.clear()
        for s in self._sessions:
            focused = s.session_id == self._focused_sid
            marker = "👉 " if focused else ""
            style = f"reverse {_STATUS_STYLE[s.status]}" if focused else _STATUS_STYLE[s.status]
            suffix = f" {s.waiting_icon}" if s.status is Status.WAITING and s.waiting_icon else ""
            if s.hook_stale:
                suffix += " ⚠"
            if s.kind == "agent":
                suffix += " ⚙"
            status_cell = Text(f"{marker}{s.status.marker} {status_label(s.status)}{suffix}", style=style)
            progress = f"{s.todo[0]}/{s.todo[1]}" if s.todo else "·"
            loc_cell = f"📍{_middle_truncate(s.location, _LOC_MAX)}"
            project_cell = labeled_project(s.project, labels.get(s.session_id, ""))
            cells: list[object] = [status_cell]
            if self._show_tool:
                cells.append(provider_label(s.provider))
            cells += [project_cell, progress, _rel(s.idle_for), loc_cell, s.last_action]
            table.add_row(*cells)
        if self._sessions:
            table.move_cursor(row=min(cursor, len(self._sessions) - 1))
        self._update_detail()
        self._poll_focus_request()

    def _apply_permission_acks(self) -> None:
        """以終端回覆結果蓋過同一 revision 的 stale WAITING，直到 provider 送來新事件。"""
        present_ids = {s.session_id for s in self._sessions}
        for session_id in set(self._permission_acks) - present_ids:
            del self._permission_acks[session_id]

        for s in self._sessions:
            acknowledged_revision = self._permission_acks.get(s.session_id)
            if acknowledged_revision is None:
                continue
            if s.last_active > acknowledged_revision:
                # 新 hook event（可能是下一個權限請求）已到，重新採信 provider 狀態。
                del self._permission_acks[s.session_id]
                continue
            if s.status is Status.WAITING:
                # 終端畫面已證實原對話框消失；provider 尚未回報後續狀態時先視為處理中。
                s.status = Status.WORKING
                s.waiting_kind = ""
                s.waiting_detail = ""
            else:
                # provider 已用同 revision 清掉 WAITING，不再需要本地 acknowledgment。
                del self._permission_acks[s.session_id]
        self._sessions.sort(key=lambda s: (s.status.rank, s.idle_for))

    def _update_detail(self) -> None:
        """detail 列：選中的 session 在 🔴 等什麼（hook 有給具體內容才顯示）。

        讓你不必跳過去就知道「哦是要跑這個指令的權限」——小事可以先放著。
        """
        s = self._selected()
        widget = self.query_one("#detail", Static)
        if s is not None and s.hook_stale:
            widget.update(Text(_("  ⚠ hook 可能失效：來源檔有更新但沒有 heartbeat"), style="yellow"))
            return
        if s is not None and s.status is Status.WAITING and s.waiting_detail:
            icon = s.waiting_icon or "🔴"
            widget.update(Text(f"  {icon} {s.waiting_detail}", style=_STATUS_STYLE[Status.WAITING]))
        elif s is not None and s.kind == "agent":
            widget.update(Text(f"  ⚙ {_agent_resume_hint(s.session_id)}", style="grey50"))
        else:
            widget.update("")

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        # 游標移動（↑/↓/j/k）時同步 detail 列，不必等下一次刷新。
        # RowHighlighted 是非同步訊息，可能在 app 收場、widget 已卸載後才送達——安靜跳過。
        try:
            self._update_detail()
        except Exception:
            pass

    def _selected(self) -> Session | None:
        row = self.query_one(DataTable).cursor_row
        if 0 <= row < len(self._sessions):
            return self._sessions[row]
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        # DataTable 被 focus 時會吃掉 Enter（變成 RowSelected），在這裡接、轉成跳轉。
        self.action_jump()

    def action_name_session(self) -> None:
        s = self._selected()
        if s is None:
            self._set_status(_("（沒有選到 session）"))
            return

        def _save(label: str | None) -> None:
            if label is None:
                return  # Esc 取消，不動
            set_label(s.session_id, label)
            self._reload()

        self.push_screen(_NameModal(s.project, get_label(s.session_id)), _save)

    def action_refresh_now(self) -> None:
        self._clear_delete_armed()
        self._reload()

    def action_toggle_all(self) -> None:
        self._clear_delete_armed()
        self._show_all = not self._show_all
        self._reload()

    def action_jump(self) -> None:
        self._clear_delete_armed()
        s = self._selected()
        if s is None:
            self._set_status(_("（沒有選到 session）"))
            return
        if s.session_id == self._focused_sid:
            self._focused_sid = None  # 你已親自跳過去處理，解除通知標記
        name = self._display_name(s)
        if s.kind == "agent" and not s.tmux_target and not s.tty:
            # 背景 agent 本就無終端可跳，不呼叫 focus_jump（沒有畫面可假裝跳得過去）。
            text = _("{project}：{msg}", project=name, msg=_agent_resume_hint(s.session_id))
            self._set_status(text)
            self.notify(text, severity="warning", timeout=10)
            return
        ok, msg = focus_jump(s)
        if ok:
            text = _("→ {project}（{where}）", project=name, where=msg)
        else:
            text = _("{project}：{msg}", project=name, msg=msg)
        self._set_status(text)
        self.notify(text, severity="information" if ok else "warning", timeout=10)

    def action_jump_oldest_waiting(self) -> None:
        self._clear_delete_armed()
        candidates = [(idx, s) for idx, s in enumerate(self._sessions) if s.status is Status.WAITING]
        if not candidates:
            self._set_status(_("（沒有正在等待的 session）"))
            return
        idx, _session = max(candidates, key=lambda item: item[1].idle_for)
        self.query_one(DataTable).move_cursor(row=idx)
        self.action_jump()

    def action_permission_reply(self) -> None:
        """游標列的 session 掛著權限對話框時，就地讀出選項讓你選、代按，不必跳過去。

        支援 tmux 內的 session，以及（macOS 上）直接開在 iTerm2 分頁、沒有 tmux 的
        session——backend 選擇見 ``permission.select_backend``。解析不到可辨識的權限
        對話框就只 toast、絕不送鍵——沒有對話框時按鍵會落進聊天輸入框變成文字。
        """
        self._clear_delete_armed()
        s = self._selected()
        if s is None:
            self._set_status(_("（沒有選到 session）"))
            return
        name = self._display_name(s)
        backend = permission.select_backend(s)
        if backend is None:
            if s.kind == "agent":
                self._toast(_("{project}：{msg}", project=name, msg=_agent_resume_hint(s.session_id)))
            else:
                self._toast(
                    _(
                        "{project}：沒有 tmux 座標，且非 macOS 上的 iTerm2 session，無法就地回覆",
                        project=name,
                    )
                )
            return
        screen = backend.capture()
        if screen is None:
            self._toast(_("{project}：讀不到 {backend} 畫面", project=name, backend=backend.name))
            return
        dialog = permission.parse_permission_dialog(screen)
        if dialog is None:
            self._toast(_("{project}：畫面上沒有可辨識的權限對話框，未送出任何按鍵", project=name))
            return

        def _submit(number: int | None) -> None:
            if number is None:
                return  # Esc 取消，不送
            self._finish_permission_reply(s, name, backend, dialog, number)

        self.push_screen(_PermissionModal(name, dialog), _submit)

    def _toast(self, text: str, *, ok: bool = False) -> None:
        """status 列＋toast 一次到位（權限回覆路徑的訊息都成對出現）。"""
        self._set_status(text)
        self.notify(text, severity="information" if ok else "warning", timeout=8)

    def _finish_permission_reply(
        self,
        s: Session,
        name: str,
        backend: permission.PermissionBackend,
        dialog: permission.PermissionDialog,
        number: int,
    ) -> None:
        """浮層選定後：再驗證一次、送鍵、依結果回報（見 permission.send_permission_reply）。"""
        outcome = permission.send_permission_reply(backend, dialog, number)
        option = next((f"{n}. {text}" for n, text in dialog.options if n == number), str(number))
        if outcome is permission.ReplyOutcome.OK:
            if s.session_id == self._focused_sid:
                self._focused_sid = None  # 已就地回覆，解除通知標記
            self._permission_acks[s.session_id] = s.last_active
            self._toast(_("→ {project}：已回覆權限（{option}）", project=name, option=option), ok=True)
            self._reload()
            return
        messages = {
            permission.ReplyOutcome.NO_DIALOG: _("{project}：權限對話框已不在，未送出任何按鍵", project=name),
            permission.ReplyOutcome.CHANGED: _("{project}：對話框內容已變化，未送出；請再按一次 p", project=name),
            permission.ReplyOutcome.SEND_FAILED: _("{project}：tmux send-keys 失敗，沒能回覆", project=name),
            permission.ReplyOutcome.STILL_PRESENT: _("{project}：已送出但對話框還在，請跳過去確認", project=name),
            permission.ReplyOutcome.MISFIRE: _(
                "{project}：對話框已不在，數字落進輸入框，已補 Backspace 清掉", project=name
            ),
            permission.ReplyOutcome.UNVERIFIED: _("{project}：已送出但拿不到畫面驗證，請跳過去確認", project=name),
        }
        self._toast(messages[outcome])

    def action_delete_session(self) -> None:
        s = self._selected()
        if s is None:
            self._set_status(_("（沒有選到 session）"))
            return

        now = time.monotonic()
        name = self._display_name(s)
        if self._delete_armed_sid != s.session_id or now > self._delete_armed_until:
            self._delete_armed_sid = s.session_id
            self._delete_armed_until = now + 2.0
            self._set_status(_("再按一次 d 隱藏 {project}（有新活動會自動重新出現）", project=name))
            return

        self._clear_delete_armed()
        if s.source == "proc" and s.session_id.startswith("synthetic:"):
            self._set_status(_("{project} 是仍在執行的 process，無法隱藏；請跳過去結束它", project=name))
            return
        hide_session(s.session_id)
        deleted = delete_session_state(s.session_id)
        set_label(s.session_id, "")
        if s.session_id == self._focused_sid:
            self._focused_sid = None
        text = (
            _("已隱藏 {project}，並清掉 RiNG 狀態；有新活動會自動重新出現", project=name)
            if deleted
            else _("已隱藏 {project}（沒有 RiNG registry 可清；有新活動會自動重新出現）", project=name)
        )
        self._reload()
        self._set_status(text)


def run_tui(interval: float = 2.0, show_all: bool = False) -> int:
    RingApp(interval=interval, show_all=show_all).run()
    return 0
