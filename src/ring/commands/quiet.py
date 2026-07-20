"""``ring quiet`` command handler：暫時全域靜音（跟 debounce 共用同一份 queue／flush 機制）。"""

from __future__ import annotations

import sys
import time

from ring.commands._args import strip_lang
from ring.gc import parse_duration
from ring.i18n import gettext as _
from ring.notify_queue import clear_quiet, flush_if_due, format_remaining, quiet_active, quiet_remaining, set_quiet


def _print_status(now: float) -> None:
    if not quiet_active(now):
        print(_("🔈 quiet：目前關閉"))
        return
    remaining = quiet_remaining(now)
    if remaining is None:
        print(_("🔇 quiet：開啟中（手動解除前一直靜音）"))
    else:
        print(_("🔇 quiet：開啟中，剩 {remaining}", remaining=format_remaining(remaining)))


def run_quiet(args: list[str]) -> int:
    """不帶參數→顯示現況；``on``→無限靜音；``off``→解除並立即 flush；``<duration>``→限時靜音。"""
    args = strip_lang(args)
    now = time.time()

    if not args:
        _print_status(now)
        return 0

    action = args[0]
    if action == "on":
        set_quiet(None)
        print(_("🔇 quiet 已開啟（手動解除前一直靜音）"))
        return 0

    if action == "off":
        clear_quiet()
        flush_if_due(force=True)
        print(_("🔈 quiet 已解除"))
        return 0

    try:
        seconds = parse_duration(action)
    except ValueError:
        print(_("無效的 duration：{value}（例如 30m、1h）", value=action), file=sys.stderr)
        return 2

    set_quiet(now + seconds)
    print(_("🔇 quiet 已開啟，{duration} 後自動解除", duration=action))
    return 0
