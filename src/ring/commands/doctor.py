"""``ring doctor`` command handler."""

from __future__ import annotations

import shutil
import sys

from ring.agent_notify import native_notify_status
from ring.commands._args import strip_lang
from ring.config import CONFIG_PATH, get_config
from ring.focus.kitty import sockets as kitty_sockets
from ring.gc import DEFAULT_OLDER_THAN_SECONDS
from ring.gc import collect_candidates as gc_collect_candidates
from ring.i18n import gettext as _
from ring.sources import discover_sessions, sources


def _print_native_notify() -> None:
    """印「Claude Code 自帶通知」這一節。

    這節回答的是「除了 RiNG，還有誰會跳通知」——``ring install-hooks`` 不會關掉
    Claude Code 自己的 ``preferredNotifChannel``（見 ``ring.agent_notify`` 模組說明），
    兩邊會對同一個權限請求各發一則，而且 Claude Code 那條還會多發 Stop 之後的
    idle_prompt。整節唯讀，任何一步失敗就整節略過，不影響其餘報告。
    """
    try:
        status = native_notify_status()
    except Exception:
        print(f"  {_('狀態')}：{_('偵測失敗')}")
        return

    # 用 "Claude 設定檔" 而不是既有的 "設定檔" msgid：後者是最後一節 RiNG 自己 config
    # 的標題（en 翻成 "Config File"），共用會把這行講成 RiNG 的設定檔，指錯地方。
    print(f"  {_('Claude 設定檔')}：{status.path}")
    if not status.exists:
        print(f"  {_('狀態')}：{_('settings 檔不存在 → 吃 Claude Code 內建預設')}")
    channel_str = status.channel if status.explicit else _("未設定（＝{channel}）", channel=status.channel)
    print(f"  preferredNotifChannel：{channel_str}")
    print(f"  {_('目前終端')}：{status.terminal or _('認不出（tmux／ssh 裡可能測不到，僅供參考）')}")

    if status.kind == "off":
        print(f"  {_('狀態')}：{_('已關閉——只有 RiNG 會通知你')}")
        return
    if status.kind == "bell":
        print(f"  {_('狀態')}：{_('只響鈴、不跳通知框；RiNG 的通知不受影響')}")
        return
    if status.kind == "banner":
        print(f"  {_('狀態')}：{_('Claude Code 也會自己跳通知——權限提示會跟 RiNG 重複')}")
    else:
        print(f"  {_('狀態')}：{_('認不出這個通道會不會跳通知；沒明確關掉就有重複的可能')}")
    print(f"  {_('另外')}：{_('它還會在 Stop 後約 60 秒發 idle_prompt（閒著、換你），這類 RiNG 刻意不發')}")
    print(f"  {_('只想留 RiNG')}：{_('把 preferredNotifChannel 設成 notifications_disabled')}")


def run_doctor(args: list[str]) -> int:
    """唯讀環境診斷，印出各節報告，固定回 0。args 非空回 2。"""
    args = strip_lang(args)
    if args:
        print(_("用法：ring doctor"), file=sys.stderr)
        return 2

    from ring.focus import focusers
    from ring.hook import hook_status
    from ring.notify import _select_notifier, notifiers
    from ring.osascript import osascript

    cfg = get_config()

    print(_("RiNG 環境診斷"))
    print(f"  {_('狀態')}：{_('唯讀檢查，不會改動任何設定')}")
    print()

    print(_("Session 來源"))
    src_list = sources()
    width_src = max(len(s.name) for s in src_list) if src_list else 10
    for src in src_list:
        try:
            found = src.discover()
            diagnostic = getattr(src, "diagnostic_issue", None)
            issue = diagnostic() if callable(diagnostic) else ""
            if issue:
                print(f"  {src.name:<{width_src}}  {_('偵測失敗')} ({issue})")
                continue
            n = len(found)
            status_str = _("活著")
            count_str = _("偵測到 {n} 個 session", n=n)
            print(f"  {src.name:<{width_src}}  {status_str}    {count_str}")
        except Exception:
            print(f"  {src.name:<{width_src}}  {_('偵測失敗')}")
    print()

    print(_("Hook 安裝"))
    statuses = hook_status()
    provider_labels = {"claude-code": "Claude Code", "codex": "Codex"}
    width_hook = max(len(provider_labels.get(s.provider, s.provider)) for s in statuses) if statuses else 10
    for hs in statuses:
        label = provider_labels.get(hs.provider, hs.provider)
        if not hs.applicable:
            msg = _("未使用 Codex（zero-config）")
        elif hs.installed:
            msg = _("已安裝")
        else:
            msg = _("未安裝（執行 ring install-hooks）")
        print(f"  {label:<{width_hook}}  {msg}")
    print()

    print(_("Hook 心跳偵測"))
    try:
        stale_hooks = [s for s in discover_sessions() if s.hook_stale]
        if stale_hooks:
            sample = ", ".join(s.project for s in stale_hooks[:3])
            print(f"  {_('狀態')}：{_('可能失效')}（{_('{n} 個 session', n=len(stale_hooks))}：{sample}）")
        else:
            print(f"  {_('狀態')}：{_('正常')}")
    except Exception:
        print(f"  {_('狀態')}：{_('偵測失敗')}")
    print()

    print(_("通知後端"))
    print(f"  {_('目前設定')}：{cfg.notify_backend}")
    notifier_list = notifiers()
    width_n = max(len(nt.name) for nt in notifier_list) if notifier_list else 10
    for nt in notifier_list:
        avail_str = _("可用") if nt.available() else _("不可用")
        print(f"  {nt.name:<{width_n}}  {avail_str}")
    selected = _select_notifier(cfg.notify_backend)
    if selected is not None:
        print(f"  {_('auto 實際選中')}：{selected.name}")
        if sys.platform == "darwin" and selected.name in {"terminal-notifier", "osascript"}:
            print(f"  {_('macOS 提醒：若只聽到聲音但沒有通知框，請到系統設定的通知項目啟用 Banner/Alert。')}")
    else:
        if cfg.notify_backend == "none":
            reason = _("backend=none")
        elif cfg.notify_backend == "agent-hooks" and shutil.which("agent-hooks") is not None:
            reason = _("agent-hooks 已接手")
        else:
            reason = _("全部不可用")
        print(f"  {_('auto 實際選中')}：{_('不發通知')}（{reason}）")
    print()

    print(_("Claude Code 自帶通知"))
    _print_native_notify()
    print()

    print(_("聚焦終端（focuser）"))
    focuser_list = focusers()
    width_f = max(len(f.name) for f in focuser_list) if focuser_list else 10
    for f in focuser_list:
        name_lower = f.name.lower()
        if name_lower == "neovim":
            avail = shutil.which("nvim") is not None
            avail_str = _("可用") if avail else _("不可用（nvim 不在 PATH）")
        elif name_lower == "tmux":
            avail = shutil.which("tmux") is not None
            avail_str = _("可用") if avail else _("不可用（tmux 不在 PATH）")
        elif name_lower == "kitty":
            if shutil.which("kitty") is None:
                avail_str = _("不可用（kitty 不在 PATH）")
            elif not kitty_sockets():
                avail_str = _("不可用（kitty remote control 沒開：kitty.conf 要設 allow_remote_control ＋ listen_on）")
            else:
                avail_str = _("可用")
        elif name_lower == "linux-wm":
            if not sys.platform.startswith("linux"):
                avail_str = _("不可用（非 Linux）")
            elif shutil.which("wmctrl") is None:
                avail_str = _("不可用（wmctrl 不在 PATH）")
            else:
                avail_str = _("可用")
        elif shutil.which("osascript") is None:
            avail_str = _("不可用（osascript 不在 PATH）")
        else:
            app_name = f.name
            try:
                rc, out, _err = osascript(f'application "{app_name}" is running')
                avail_str = _("可用") if (rc == 0 and out == "true") else _("不可用（app 沒在跑）")
            except Exception:
                avail_str = _("不可用（app 沒在跑）")
        print(f"  {f.name:<{width_f}}  {avail_str}")
    print()

    print(_("維護"))
    try:
        candidates = gc_collect_candidates(older_than=DEFAULT_OLDER_THAN_SECONDS)
        if candidates:
            print(f"  {_('可清理')}：{_('{n} 個 RiNG stale 狀態檔（執行 ring gc --dry-run 預覽）', n=len(candidates))}")
        else:
            print(f"  {_('可清理')}：{_('沒有 RiNG stale 狀態檔')}")
    except Exception:
        print(f"  {_('可清理')}：{_('偵測失敗')}")
    print()

    print(_("設定檔"))
    exists = CONFIG_PATH.exists()
    print(f"  {_('路徑')}：{CONFIG_PATH}")
    print(f"  {_('狀態')}：{_('已存在') if exists else _('不存在（全部用內建預設）')}")
    print(f"  {_('完整生效值請看 `ring config`。')}")

    return 0
