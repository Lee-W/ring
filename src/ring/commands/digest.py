"""``ring digest`` command handler——回座時快速掃過目前狀態。"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass
from typing import Literal

from ring.commands._args import strip_lang
from ring.gc import parse_duration
from ring.i18n import gettext as _
from ring.registry import Session, Status
from ring.sources import discover_sessions
from ring.stats import collect_waits


@dataclass(frozen=True)
class Digest:
    since: str
    since_seconds: float
    generated_at: float
    waiting: list[Session]
    idle: list[Session]
    ended: list[Session]
    waits: int
    wait_seconds: float
    ongoing_waits: int


def _rel(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def _session_line(s: Session) -> str:
    detail = f" — {s.waiting_detail}" if s.waiting_detail else ""
    kind = f"{s.waiting_icon} " if s.waiting_icon else ""
    return f"{kind}{s.project} ({_rel(s.idle_for)}){detail}"


def build_digest(*, since: str, since_seconds: float, now: float | None = None) -> Digest:
    generated_at = time.time() if now is None else now
    cutoff = generated_at - since_seconds
    sessions = [s for s in discover_sessions() if s.last_active >= cutoff or s.status is Status.WAITING]
    waits = collect_waits(since_seconds, now=generated_at)
    return Digest(
        since=since,
        since_seconds=since_seconds,
        generated_at=generated_at,
        waiting=sorted((s for s in sessions if s.status is Status.WAITING), key=lambda s: s.last_active),
        idle=sorted((s for s in sessions if s.status is Status.IDLE), key=lambda s: s.last_active, reverse=True),
        ended=sorted((s for s in sessions if s.status is Status.ENDED), key=lambda s: s.last_active, reverse=True),
        waits=len(waits),
        wait_seconds=sum(w.seconds for w in waits),
        ongoing_waits=sum(1 for w in waits if w.ongoing),
    )


def render_text(digest: Digest) -> str:
    lines = [_("🎤 RiNG digest — 最近 {since}", since=digest.since)]
    if not (digest.waiting or digest.idle or digest.ended or digest.waits):
        lines.append(_("  （這段時間沒有 session 活動。）"))
        return "\n".join(lines)

    if digest.waiting:
        lines.append(_("  🔴 正在等你：{n}", n=len(digest.waiting)))
        lines.extend(f"    - {_session_line(s)}" for s in digest.waiting[:5])
    if digest.idle:
        lines.append(_("  🟡 已停著：{n}", n=len(digest.idle)))
        lines.extend(f"    - {_session_line(s)}" for s in digest.idle[:5])
    if digest.ended:
        lines.append(_("  ⚫ 已離場：{n}", n=len(digest.ended)))
        lines.extend(f"    - {_session_line(s)}" for s in digest.ended[:5])
    if digest.waits:
        lines.append(_("  等待統計：{n} 次，共 {duration}", n=digest.waits, duration=_rel(digest.wait_seconds)))
        if digest.ongoing_waits:
            lines.append(_("  其中 {n} 段還在等你。", n=digest.ongoing_waits))
    return "\n".join(lines)


def render_json(digest: Digest) -> str:
    def _pack(s: Session) -> dict[str, object]:
        return {
            "session_id": s.session_id,
            "project": s.project,
            "status": s.status.value,
            "last_active": s.last_active,
            "idle_seconds": round(s.idle_for, 1),
            "waiting_kind": s.waiting_kind,
            "waiting_detail": s.waiting_detail,
            "source": s.source,
        }

    return json.dumps(
        {
            "generated_at": digest.generated_at,
            "since": digest.since,
            "since_seconds": digest.since_seconds,
            "waiting": [_pack(s) for s in digest.waiting],
            "idle": [_pack(s) for s in digest.idle],
            "ended": [_pack(s) for s in digest.ended],
            "waits": {
                "count": digest.waits,
                "total_seconds": round(digest.wait_seconds, 1),
                "ongoing": digest.ongoing_waits,
            },
        },
        ensure_ascii=False,
        indent=2,
    )


def run_digest(args: list[str]) -> int:
    args = strip_lang(args)
    parser = argparse.ArgumentParser(prog="ring digest", description=_("離席摘要：彙整最近一段時間的 session 狀態。"))
    parser.add_argument(
        "--since",
        default="4h",
        metavar="DURATION",
        help=_("摘要時間窗（例如 30m、4h、1d；預設 4h）"),
    )
    parser.add_argument("--format", choices=["text", "json"], default="text", help=_("輸出格式：text / json"))
    try:
        ns = parser.parse_args(args)
        since_seconds = parse_duration(ns.since)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2
    except ValueError:
        print(_("無效的 --since：{value}", value=ns.since), file=sys.stderr)
        return 2

    digest = build_digest(since=ns.since, since_seconds=since_seconds)
    fmt: Literal["text", "json"] = ns.format
    print(render_json(digest) if fmt == "json" else render_text(digest))
    return 0
