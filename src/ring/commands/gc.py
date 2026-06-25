"""``ring gc`` command handler."""

from __future__ import annotations

import argparse
import sys

from ring.commands._args import strip_lang
from ring.gc import DEFAULT_OLDER_THAN_SECONDS, parse_duration
from ring.gc import run_gc as gc_run
from ring.i18n import gettext as _


def run_gc(args: list[str]) -> int:
    """清理 RiNG 自己的 stale 狀態檔。"""
    args = strip_lang(args)

    parser = argparse.ArgumentParser(prog="ring gc", description=_("清理 RiNG 自己的 stale 狀態檔。"))
    parser.add_argument("--dry-run", action="store_true", help=_("只預覽，不刪檔"))
    parser.add_argument(
        "--older-than",
        default="7d",
        metavar="DURATION",
        help=_("清理超過指定時間的已離場 registry（例如 30m、2h、7d；預設 7d）"),
    )
    parser.add_argument("--all-ended", action="store_true", help=_("清理所有目前判定已離場的 registry"))
    try:
        ns = parser.parse_args(args)
        older_than = parse_duration(ns.older_than) if ns.older_than else DEFAULT_OLDER_THAN_SECONDS
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    except ValueError:
        print(_("無效的 --older-than：{value}", value=ns.older_than), file=sys.stderr)
        return 2

    result = gc_run(older_than=older_than, all_ended=ns.all_ended, dry_run=ns.dry_run)
    action = _("將刪除") if result.dry_run else _("已刪除")
    count = len(result.candidates) if result.dry_run else len(result.deleted)
    print(_("RiNG GC：{action} {count} 個檔案", action=action, count=count))
    shown = result.candidates if result.dry_run else result.deleted
    for candidate in shown:
        prefix = "  - " if result.dry_run else "  ✓ "
        print(f"{prefix}{candidate.path} ({candidate.reason})")
    for candidate, error in result.errors:
        print(f"  ! {candidate.path} ({error})", file=sys.stderr)
    return 1 if result.errors else 0
