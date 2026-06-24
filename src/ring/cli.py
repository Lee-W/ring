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
from ring.config import CONFIG_PATH, Config, ConfigError, get_config, set_value
from ring.i18n import gettext as _
from ring.i18n import ngettext, set_lang
from ring.labels import load_labels
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


def provider_label(provider: str) -> str:
    """把內部 provider 值轉成畫面上的工具名稱（品牌名不翻譯）。"""
    return {"claude": "Claude", "claude-code": "Claude", "codex": "Codex"}.get(
        provider, provider.title() if provider else "—"
    )


def labeled_project(project: str, label: str) -> str:
    """專案名後接使用者自訂標籤（有的話）：``maigo · 重構登入``。"""
    return f"{project} · {label}" if label else project


def show_tool_column(sessions: list[Session]) -> bool:
    """有混用工具（>1 種 provider）時才需要工具欄；全是同一種就省掉。"""
    return len({s.provider for s in sessions}) > 1


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


def _rich_renderable(sessions: list[Session], show_legend: bool, show_tool: bool = True) -> Group:
    pids = running_agent_pids()
    blocks: list[Any] = [Text(_header(len(sessions), len(pids)), style="bold")]
    if show_legend:
        blocks.append(_rich_legend())
    if not sessions:
        blocks.append(Text("  " + _("（場館目前沒人上台）"), style=f"{_MUTED} italic"))
        return Group(*blocks)

    table = Table(box=SIMPLE_HEAD, header_style="bold", pad_edge=False, expand=False)
    table.add_column(_("狀態"), no_wrap=True, min_width=9)
    if show_tool:
        table.add_column(_("工具"), no_wrap=True)
    table.add_column(_("專案"), style=_COLORS["project"], no_wrap=True)
    table.add_column(_("進度"), justify="right", no_wrap=True)
    table.add_column(_("閒置"), justify="right", no_wrap=True)
    table.add_column(_("去哪"), style=_COLORS["location"], no_wrap=True, min_width=16, max_width=_LOC_MAX)
    # action 可能很長：給 max_width 上限，否則 no_wrap 會吃掉整列寬度、把其他欄壓成 0。
    table.add_column(_("動作"), no_wrap=True, overflow="ellipsis", max_width=50)

    labels = load_labels()
    for s in sessions:
        status_cell = Text(f"{s.status.marker} {status_label(s.status)}", style=_STATUS_STYLE[s.status])
        progress = f"{s.todo[0]}/{s.todo[1]}" if s.todo else "·"
        loc_cell = f"📍{_middle_truncate(s.location, _LOC_MAX)}"
        project_cell = labeled_project(s.project, labels.get(s.session_id, ""))
        cells: list[Any] = [status_cell]
        if show_tool:
            cells.append(provider_label(s.provider))
        cells += [project_cell, progress, _rel(s.idle_for), loc_cell, s.last_action]
        table.add_row(*cells)

    blocks.append(table)
    return Group(*blocks)


# ----------------------------------------------------------------------------- plain fallback
def _render_plain(sessions: list[Session], show_legend: bool, show_tool: bool = True) -> str:
    pids = running_agent_pids()
    lines = [_header(len(sessions), len(pids))]
    if show_legend:
        items = "   ".join(f"{st.marker} {status_label(st)}" for st in Status)
        lines += ["", f"  {_('圖例')}   {items}"]
    if not sessions:
        lines += ["", "  " + _("（場館目前沒人上台）")]
        return "\n".join(lines)

    labels = load_labels()
    rows = [
        (
            s.status.marker,
            provider_label(s.provider),
            labeled_project(s.project, labels.get(s.session_id, "")),
            f"{s.todo[0]}/{s.todo[1]}" if s.todo else "·",
            _rel(s.idle_for),
            _middle_truncate(s.location, _LOC_MAX),
            s.last_action[:48],
        )
        for s in sessions
    ]
    c_tool, c_proj, c_prog, c_idle, c_loc, c_act = _("工具"), _("專案"), _("進度"), _("閒置"), _("去哪"), _("動作")
    w_tool = max(len(c_tool), *(len(r[1]) for r in rows))
    w_proj = max(len(c_proj), *(len(r[2]) for r in rows))
    w_prog = max(len(c_prog), *(len(r[3]) for r in rows))
    w_ago = max(len(c_idle), 3, *(len(r[4]) for r in rows))
    w_loc = max(len(c_loc), *(len(r[5]) for r in rows))
    tool_h = f"{c_tool:<{w_tool}}  " if show_tool else ""
    header = (
        f"     {tool_h}{c_proj:<{w_proj}}  {c_prog:>{w_prog}}  "
        f"{c_idle:>{w_ago}}    {c_loc:<{w_loc}}  {c_act}"
    )
    lines += ["", header]
    for marker, tool, project, prog, ago, loc, action in rows:
        tool_c = f"{tool:<{w_tool}}  " if show_tool else ""
        lines.append(
            f"  {marker} {tool_c}{project:<{w_proj}}  {prog:>{w_prog}}  "
            f"{ago:>{w_ago}}  📍{loc:<{w_loc}}  {action}"
        )
    return "\n".join(lines)


# ----------------------------------------------------------------------------- entry
def print_snapshot(sessions: list[Session], show_legend: bool) -> None:
    show_tool = show_tool_column(sessions)
    if HAVE_RICH:
        Console().print(_rich_renderable(sessions, show_legend, show_tool))
    else:
        print(_render_plain(sessions, show_legend, show_tool))


def _format_config_value(value: object) -> str:
    """把一個設定值轉成單行可讀字串（None → —、空 tuple → 內建預設、dict → k=v 串）。"""
    if value is None:
        return "—"
    if isinstance(value, tuple):
        return "[" + ", ".join(str(v) for v in value) + "]" if value else _("（內建預設）")
    if isinstance(value, dict):
        return ", ".join(f"{k}={v}" for k, v in value.items())
    return str(value)


def print_config() -> None:
    """印出設定檔位置與目前生效的所有設定（覆寫過的標 ←）。

    欄位直接從 ``Config`` dataclass 列舉，所以新增設定不必再動這裡。值跟內建預設
    不同的會標一個箭頭，讓你一眼看出「我改過哪些」。
    """
    from dataclasses import fields

    cfg = get_config()
    defaults = Config()
    exists = CONFIG_PATH.exists()

    print(_("RiNG 設定檔"))
    print(f"  {_('路徑')}：{CONFIG_PATH}")
    print(f"  {_('狀態')}：{_('已存在') if exists else _('不存在（全部用內建預設）')}")
    print()
    print(_("目前生效的設定（← = 你覆寫過的）"))
    width = max(len(f.name) for f in fields(cfg))
    for f in fields(cfg):
        value = getattr(cfg, f.name)
        overridden = value != getattr(defaults, f.name)
        mark = "  ←" if overridden else ""
        print(f"  {f.name:<{width}}  {_format_config_value(value)}{mark}")
    print()
    hint = _("用 `ring config set KEY VALUE` 改，或直接編輯上面那個檔；完整選項見 src/ring/config.py 的 docstring。")
    print("  " + hint)


def _config_get_value(key: str) -> object:
    """讀單一設定的目前生效值（支援 colors.<name> 點記法）。未知鍵丟 ConfigError。"""
    from dataclasses import fields

    cfg = get_config()
    if "." in key:
        table, sub = key.split(".", 1)
        if table == "colors" and sub in cfg.colors:
            return cfg.colors[sub]
        raise ConfigError(_("未知的鍵：{key}", key=key))
    if key in {f.name for f in fields(cfg)}:
        return getattr(cfg, key)
    raise ConfigError(_("未知的鍵：{key}", key=key))


def _strip_lang(args: list[str]) -> list[str]:
    """濾掉全域 ``--lang`` 旗標（已在 main 先 peek 過），只留 config 自己的位置參數。"""
    out: list[str] = []
    skip = False
    for i, a in enumerate(args):
        if skip:
            skip = False
            continue
        if a == "--lang":
            skip = i + 1 < len(args)  # 連同它的值一起跳過
            continue
        if a.startswith("--lang="):
            continue
        out.append(a)
    return out


def run_config(args: list[str]) -> int:
    """``ring config`` 進入點：無參數→列表；``get KEY``→讀；``set KEY VALUE``→寫。"""
    args = _strip_lang(args)
    if not args:
        print_config()
        return 0

    action, rest = args[0], args[1:]
    if action == "get":
        if len(rest) != 1:
            print(_("用法：ring config get KEY"), file=sys.stderr)
            return 2
        try:
            print(_format_config_value(_config_get_value(rest[0])))
        except ConfigError as e:
            print(f"⚠️ {e}", file=sys.stderr)
            return 1
        return 0

    if action == "set":
        if len(rest) != 2:
            print(_("用法：ring config set KEY VALUE"), file=sys.stderr)
            return 2
        key, value = rest
        try:
            coerced = set_value(key, value)
        except ConfigError as e:
            print(f"⚠️ {e}", file=sys.stderr)
            return 1
        print(_("✅ 已設定 {key} = {value}（{path}）", key=key, value=_format_config_value(coerced), path=CONFIG_PATH))
        print("   " + _("註：set 會重寫整個設定檔，原有註解不會保留。"))
        return 0

    print(_("未知的 config 動作：{action}（用 get / set，或不帶參數看目前設定）", action=action), file=sys.stderr)
    return 2


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
                print(_render_plain(sessions, show_legend, show_tool_column(sessions)))
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
                body = _rich_renderable(sessions, show_legend, show_tool_column(sessions))
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


def _commands_help() -> str:
    return _(
        """
commands:
  hook [PROVIDER]              從 stdin 讀 provider hook payload，寫入 RiNG registry
  hook --provider PROVIDER     同上，明確指定 provider（例如 codex）
  install-hooks [--dry-run]    安裝 Claude Code hooks
  remove-hooks [--dry-run]     移除 Claude Code hooks
  config                       顯示設定檔位置與目前生效的設定
  config get KEY               讀單一設定的目前值
  config set KEY VALUE         寫入單一設定（會重寫設定檔，不保留註解）
  focus SESSION_ID             聚焦指定 session；TUI 在跑時會回到 RiNG 並選中該列
"""
    )


def _subcommand_help(name: str) -> str:
    helps = {
        "hook": _(
            """usage: ring hook [PROVIDER] [--provider PROVIDER]

從 stdin 讀 hook JSON，依 provider 正規化後寫入 RiNG registry。
"""
        ),
        "install-hooks": _(
            """usage: ring install-hooks [--dry-run]

安裝 Claude Code hooks 到 ~/.claude/settings.json。
"""
        ),
        "remove-hooks": _(
            """usage: ring remove-hooks [--dry-run]

從 ~/.claude/settings.json 移除 RiNG 安裝的 Claude Code hooks。
"""
        ),
        "config": _(
            """usage: ring config [get KEY | set KEY VALUE]

不帶參數：顯示設定檔位置（~/.config/ring/config.toml）與目前生效的所有設定。
  get KEY        印出單一設定的目前值（colors 子鍵用 colors.<name>）。
  set KEY VALUE  寫入單一設定。注意：會重寫整個設定檔，原有註解不會保留。
"""
        ),
        "focus": _(
            """usage: ring focus SESSION_ID

聚焦指定 session；若 RiNG TUI 正在執行，會回到 TUI 並選中該列。
"""
        ),
    }
    return helps.get(name, "")


def main(argv: list[str] | None = None) -> int:
    raw = list(sys.argv[1:] if argv is None else argv)
    cfg = get_config()
    set_lang(_peek_lang(raw) or cfg.lang)  # 在 import ring.tui 前設好，Footer 按鍵說明也跟著語言

    if raw and raw[0] in {"hook", "install-hooks", "remove-hooks", "config", "focus"} and any(
        arg in {"-h", "--help"} for arg in raw[1:]
    ):
        print(_subcommand_help(raw[0]), end="")
        return 0

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
    if raw and raw[0] == "config":
        return run_config(raw[1:])
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

    parser = argparse.ArgumentParser(
        prog="ring",
        description=_("看所有 agent CLI session 上台。"),
        epilog=_commands_help(),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
