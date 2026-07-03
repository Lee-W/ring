"""``ring doctor`` command handler."""

from __future__ import annotations

import shutil
import sys

from ring.commands._args import strip_lang
from ring.config import CONFIG_PATH, get_config
from ring.gc import DEFAULT_OLDER_THAN_SECONDS
from ring.gc import collect_candidates as gc_collect_candidates
from ring.i18n import gettext as _
from ring.sources import sources


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
