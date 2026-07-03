"""``ring stats`` command handler——最近一段時間你讓 agent 🔴 等了多久。"""

from __future__ import annotations

import argparse
import sys

from ring.commands._args import strip_lang
from ring.gc import parse_duration
from ring.i18n import gettext as _
from ring.stats import ProjectStats, aggregate, collect_waits


def _fmt(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _totals(stats: list[ProjectStats]) -> ProjectStats:
    total = ProjectStats(project=_("全部"))
    for st in stats:
        total.waits += st.waits
        total.total_seconds += st.total_seconds
        total.max_seconds = max(total.max_seconds, st.max_seconds)
        total.ongoing += st.ongoing
    return total


def run_stats(args: list[str]) -> int:
    """彙報等待統計；沒資料時提示 stats 需要 hook 模式。"""
    args = strip_lang(args)
    parser = argparse.ArgumentParser(prog="ring stats", description=_("等待統計：你讓 agent 🔴 等了多久。"))
    parser.add_argument(
        "--since",
        default="7d",
        metavar="DURATION",
        help=_("統計時間窗（例如 12h、7d、30d；預設 7d）"),
    )
    try:
        ns = parser.parse_args(args)
        since = parse_duration(ns.since)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    except ValueError:
        print(_("無效的 --since：{value}", value=ns.since), file=sys.stderr)
        return 2

    spans = collect_waits(since)
    print(_("🎤 RiNG stats — 最近 {since} 的 🔴 等待", since=ns.since))
    if not spans:
        print(f"  {_('（這段時間沒有等待紀錄。stats 需要 hook 模式：ring install-hooks）')}")
        return 0

    stats = aggregate(spans)
    rows = [*stats, _totals(stats)]
    c_proj, c_n, c_avg, c_max, c_total = _("專案"), _("次數"), _("平均"), _("最長"), _("總等待")
    cells = [(s.project, str(s.waits), _fmt(s.avg_seconds), _fmt(s.max_seconds), _fmt(s.total_seconds)) for s in rows]
    w = [max(len(h), *(len(c[i]) for c in cells)) for i, h in enumerate((c_proj, c_n, c_avg, c_max, c_total))]
    print()
    print(f"  {c_proj:<{w[0]}}  {c_n:>{w[1]}}  {c_avg:>{w[2]}}  {c_max:>{w[3]}}  {c_total:>{w[4]}}")
    for i, c in enumerate(cells):
        if i == len(cells) - 1:
            print()  # 總計列前空一行
        print(f"  {c[0]:<{w[0]}}  {c[1]:>{w[1]}}  {c[2]:>{w[2]}}  {c[3]:>{w[3]}}  {c[4]:>{w[4]}}")

    ongoing = sum(s.ongoing for s in stats)
    if ongoing:
        print()
        print("  " + _("⚠️ 其中 {n} 段還在 🔴 等你（計到現在為止）。", n=ongoing))
    return 0
