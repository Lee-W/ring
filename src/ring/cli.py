"""RiNG CLI 進場口。

``ring``              印一張當下快照（Rich 表格；沒裝 rich 退回樸素版）。
``ring --watch``      像 watch 一樣持續刷新。
``ring --all``        連已離場的 session 也顯示。
``ring --no-legend``  關掉顏色圖例。
``ring --lang en``    切語言（也吃 RING_LANG / LANG）。
"""

from __future__ import annotations

import argparse
import sys
import time
from importlib.util import find_spec
from typing import Any

from ring import __version__
from ring.config import get_config
from ring.i18n import gettext as _
from ring.i18n import ngettext, set_lang
from ring.registry import Session, Status, running_agent_pids
from ring.sources import discover_sessions

try:
    from rich.box import SIMPLE_HEAD
    from rich.console import Console, Group
    from rich.live import Live
    from rich.table import Table
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # pragma: no cover - fallback path
    HAVE_RICH = False

HAVE_TEXTUAL = find_spec("textual") is not None

# 狀態 → Rich 樣式（可在 config 的 [colors] 逐項覆寫）。預設避開 dim / ANSI blue（深底會糊）。
_COLORS = get_config().colors
_STATUS_STYLE = {
    Status.WAITING: _COLORS["waiting"],
    Status.WORKING: _COLORS["working"],
    Status.IDLE: _COLORS["idle"],
    Status.ENDED: _COLORS["ended"],
}
_MUTED = _COLORS["muted"]


def _rel(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


# 「去哪」欄路徑上限，三套渲染共用（Rich / plain / TUI）
_LOC_MAX = 40


def _middle_truncate(text: str, max_len: int) -> str:
    """中段省略，保留路徑最後一層目錄完整。

    截斷規則（四分支）：

    1. ``len(text) <= max_len`` → 原樣回傳。
       tmux 座標（如 ``main:1.0``）很短，自然走這條，不被動到。

    2. 否則以 ``/`` 為界，保留最後一層目錄完整：
       ``tail = "/" + text.rsplit("/", 1)[-1]``，
       ``head_budget = max_len - 1 - len(tail)``（1 = ``…`` 的長度）。
       若 ``head_budget >= 1``：回傳 ``text[:head_budget] + "…" + tail``。

    3. ``head_budget < 1``（最後一層目錄名本身就超長，或 text 無 ``/``）：
       退化為純字元中段省略——
       ``keep = max_len - 1``；``front = (keep + 1) // 2``；``back = keep // 2``；
       回傳 ``text[:front] + "…" + text[-back:]``（總長 == max_len）。

    4. ``max_len <= 1`` 邊界：直接回傳 ``text[:max_len]``，避免負數切片。

    ``…`` 用單一字元（U+2026），長度算 1，與 Rich ``overflow="ellipsis"`` 視覺一致。
    """
    if max_len <= 1:
        return text[:max_len]
    if len(text) <= max_len:
        return text
    # 以 / 為界，保留最後一層
    tail = "/" + text.rsplit("/", 1)[-1]
    head_budget = max_len - 1 - len(tail)  # 1 for "…"
    if head_budget >= 1:
        return text[:head_budget] + "…" + tail
    # 病態長尾段 fallback：純字元中段省略
    keep = max_len - 1
    front = (keep + 1) // 2
    back = keep // 2
    if back == 0:
        return text[:max_len]
    return text[:front] + "…" + text[-back:]


def board(show_all: bool) -> list[Session]:
    sessions = discover_sessions()
    if show_all:
        return sessions
    return [s for s in sessions if s.status is not Status.ENDED]


def status_label(status: Status) -> str:
    return {
        Status.WAITING: _("等你"),
        Status.WORKING: _("工作中"),
        Status.IDLE: _("跑完停著"),
        Status.ENDED: _("已離場"),
    }[status]


def _header(n: int, pids: int) -> str:
    sess = ngettext("{n} 個 session 在場", "{n} 個 session 在場", n, n=n)
    proc = ngettext("{n} 個 agent process 跑著", "{n} 個 agent process 跑著", pids, n=pids)
    return _("🎤 RiNG — {sess} · {proc}", sess=sess, proc=proc)


# ----------------------------------------------------------------------------- Rich
def _rich_legend() -> Text:
    parts = [Text(f"{_('圖例')}   ", style=_MUTED)]
    for status in (Status.WAITING, Status.WORKING, Status.IDLE, Status.ENDED):
        parts.append(Text(f"{status.marker} {status_label(status)}   ", style=_STATUS_STYLE[status]))
    return Text.assemble(*[(p.plain, p.style) for p in parts])


def _rich_renderable(sessions: list[Session], show_legend: bool) -> Group:
    pids = running_agent_pids()
    blocks: list[Any] = [Text(_header(len(sessions), len(pids)), style="bold")]
    if show_legend:
        blocks.append(_rich_legend())
    if not sessions:
        blocks.append(Text("  " + _("（場館目前沒人上台）"), style=f"{_MUTED} italic"))
        return Group(*blocks)

    table = Table(box=SIMPLE_HEAD, header_style="bold", pad_edge=False, expand=False)
    table.add_column(_("狀態"), no_wrap=True, min_width=9)
    table.add_column(_("專案"), style=_COLORS["project"], no_wrap=True)
    table.add_column(_("進度"), justify="right", no_wrap=True)
    table.add_column(_("閒置"), justify="right", no_wrap=True)
    table.add_column(_("去哪"), style=_COLORS["location"], no_wrap=True, min_width=16, max_width=_LOC_MAX)
    # action 可能很長：給 max_width 上限，否則 no_wrap 會吃掉整列寬度、把其他欄壓成 0。
    table.add_column(_("動作"), no_wrap=True, overflow="ellipsis", max_width=50)

    for s in sessions:
        status_cell = Text(f"{s.status.marker} {status_label(s.status)}", style=_STATUS_STYLE[s.status])
        progress = f"{s.todo[0]}/{s.todo[1]}" if s.todo else "·"
        loc_cell = f"📍{_middle_truncate(s.location, _LOC_MAX)}"
        table.add_row(status_cell, s.project, progress, _rel(s.idle_for), loc_cell, s.last_action)

    blocks.append(table)
    return Group(*blocks)


# ----------------------------------------------------------------------------- plain fallback
def _render_plain(sessions: list[Session], show_legend: bool) -> str:
    pids = running_agent_pids()
    lines = [_header(len(sessions), len(pids))]
    if show_legend:
        items = "   ".join(f"{st.marker} {status_label(st)}" for st in Status)
        lines += ["", f"  {_('圖例')}   {items}"]
    if not sessions:
        lines += ["", "  " + _("（場館目前沒人上台）")]
        return "\n".join(lines)

    rows = [
        (
            s.status.marker,
            s.project,
            f"{s.todo[0]}/{s.todo[1]}" if s.todo else "·",
            _rel(s.idle_for),
            _middle_truncate(s.location, _LOC_MAX),
            s.last_action[:48],
        )
        for s in sessions
    ]
    c_proj, c_prog, c_idle, c_loc, c_act = _("專案"), _("進度"), _("閒置"), _("去哪"), _("動作")
    w_proj = max(len(c_proj), *(len(r[1]) for r in rows))
    w_prog = max(len(c_prog), *(len(r[2]) for r in rows))
    w_ago = max(len(c_idle), 3, *(len(r[3]) for r in rows))
    w_loc = max(len(c_loc), *(len(r[4]) for r in rows))
    lines += ["", f"     {c_proj:<{w_proj}}  {c_prog:>{w_prog}}  {c_idle:>{w_ago}}    {c_loc:<{w_loc}}  {c_act}"]
    for marker, project, prog, ago, loc, action in rows:
        lines.append(f"  {marker} {project:<{w_proj}}  {prog:>{w_prog}}  {ago:>{w_ago}}  📍{loc:<{w_loc}}  {action}")
    return "\n".join(lines)


# ----------------------------------------------------------------------------- entry
def print_snapshot(sessions: list[Session], show_legend: bool) -> None:
    if HAVE_RICH:
        Console().print(_rich_renderable(sessions, show_legend))
    else:
        print(_render_plain(sessions, show_legend))


def watch(interval: float, count: int, show_all: bool, show_legend: bool) -> int:
    from ring.notify import notify_waiting
    from ring.watcher import WaitingAlertScheduler

    cfg = get_config()
    frames = 0
    footer_text = _("每 {interval}s 刷新 · Ctrl-C 離場", interval=int(interval))
    if not HAVE_RICH:
        scheduler = WaitingAlertScheduler(cfg.notify_repeat_seconds, cfg.notify_repeat_max)
        try:
            while True:
                sys.stdout.write("\033[2J\033[H")
                sessions = board(show_all)
                alerts = scheduler.feed(sessions)
                try:
                    hint = notify_waiting(alerts)
                    if hint:
                        print(hint)
                except Exception:
                    pass
                print(_render_plain(sessions, show_legend))
                print("\n" + footer_text)
                sys.stdout.flush()
                frames += 1
                if count and frames >= count:
                    return 0
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0

    console = Console()
    scheduler = WaitingAlertScheduler(cfg.notify_repeat_seconds, cfg.notify_repeat_max)
    try:
        with Live(console=console, screen=True, auto_refresh=False) as live:
            while True:
                sessions = board(show_all)
                alerts = scheduler.feed(sessions)
                try:
                    hint = notify_waiting(alerts)
                    if hint:
                        print(hint)
                except Exception:
                    pass
                body = _rich_renderable(sessions, show_legend)
                live.update(Group(body, Text("\n" + footer_text, style=_MUTED)), refresh=True)
                frames += 1
                if count and frames >= count:
                    return 0
                time.sleep(interval)
    except KeyboardInterrupt:
        return 0


def _peek_lang(raw: list[str]) -> str | None:
    """在建 argparse 前先抓 --lang，好讓 help 文字也能翻譯。"""
    for i, arg in enumerate(raw):
        if arg == "--lang" and i + 1 < len(raw):
            return raw[i + 1]
        if arg.startswith("--lang="):
            return arg.split("=", 1)[1]
    return None


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "hook":
        from ring.hook import run_hook

        provider = "claude-code"
        hook_args = raw[1:]
        if hook_args:
            if hook_args[0] == "--provider" and len(hook_args) >= 2:
                provider = hook_args[1]
            elif hook_args[0].startswith("--provider="):
                provider = hook_args[0].split("=", 1)[1]
            elif not hook_args[0].startswith("-"):
                provider = hook_args[0]
        return run_hook(provider=provider)
    if raw and raw[0] == "install-hooks":
        from ring.hook import install_hooks

        return install_hooks(dry_run="--dry-run" in raw)
    if raw and raw[0] == "remove-hooks":
        from ring.hook import uninstall_hooks

        return uninstall_hooks(dry_run="--dry-run" in raw)
    if raw and raw[0] == "focus" and len(raw) >= 2:
        from ring.focus import jump as focus_jump
        from ring.ipc import read_tui_presence, write_focus_request
        from ring.sources import get_by_id

        session = get_by_id(raw[1])
        if session is None:
            return 0
        presence = read_tui_presence()
        if presence is not None:
            # TUI 在跑：寫 focus-request，讓 TUI 自己移游標並 activate 視窗。
            write_focus_request(raw[1])
        else:
            # headless（沒有 TUI 在跑）：退化回現行行為——直接跳到 claude 所在終端。
            focus_jump(session)
        return 0

    cfg = get_config()
    set_lang(_peek_lang(raw) or cfg.lang)  # 在 import ring.tui 前設好，Footer 按鍵說明也跟著語言
    parser = argparse.ArgumentParser(prog="ring", description=_("看所有 agent CLI session 上台。"))
    parser.add_argument("--version", action="version", version=f"ring {__version__}")
    parser.add_argument("--watch", action="store_true", help=_("持續刷新"))
    parser.add_argument("--interval", type=float, default=cfg.interval, help=_("watch 刷新秒數"))
    parser.add_argument("--count", type=int, default=0, help=_("watch 刷新幾格後自動結束（0=無限，預設）"))
    parser.add_argument("--all", "-a", action="store_true", default=cfg.show_all, help=_("連已離場的 session 也顯示"))
    parser.add_argument(
        "--legend",
        action=argparse.BooleanOptionalAction,
        default=cfg.legend,
        help=_("顯示顏色圖例（--no-legend 關閉）"),
    )
    parser.add_argument("--lang", help=_("語言（如 en / zh-Hant；也吃 config / RING_LANG / LANG）"))
    args = parser.parse_args(raw)

    if args.watch:
        if HAVE_TEXTUAL and sys.stdout.isatty():
            from ring.tui import run_tui

            return run_tui(args.interval, args.all)
        return watch(args.interval, args.count, args.all, args.legend)
    print_snapshot(board(args.all), args.legend)
    return 0


if __name__ == "__main__":
    sys.exit(main())
