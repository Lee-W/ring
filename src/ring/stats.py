"""等待統計資料層——量化「你讓 agent 等了多久」。

``ring hook`` 在 session **狀態轉換**時 append 一行到 ``~/.config/ring/events.jsonl``
（只記轉換、不記每個事件，量小；檔案超過上限自動砍半保新）。``ring stats`` 讀這份 log，
把「轉 🔴 等你 → 下一個轉換」算成一段等待，按專案彙整。

zero-config 測不到 🔴，所以 stats 跟精準通知一樣：hook 模式才有資料。
所有寫入失敗安靜吞掉——統計是錦上添花，絕不打斷 hook 主流程。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

EVENTS_PATH = Path.home() / ".config" / "ring" / "events.jsonl"

# log 檔上限；超過就砍半保新（append 前檢查）。單行約 150 bytes，5MB ≈ 3 萬多次轉換。
_MAX_BYTES = 5 * 1024 * 1024


def log_transition(
    session_id: str,
    provider: str,
    cwd: str,
    status: str,
    *,
    path: Path | None = None,
    now: float | None = None,
) -> None:
    """append 一行狀態轉換；失敗安靜吞掉（hook 呼叫端不需要 try）。"""
    p = path or EVENTS_PATH
    line = json.dumps(
        {
            "ts": now if now is not None else time.time(),
            "session_id": session_id,
            "provider": provider,
            "cwd": cwd,
            "status": status,
        },
        ensure_ascii=False,
    )
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        _trim_if_oversized(p)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _trim_if_oversized(p: Path) -> None:
    """log 超過 ``_MAX_BYTES`` 時砍半保新，避免無上限成長。"""
    try:
        if p.stat().st_size <= _MAX_BYTES:
            return
    except OSError:
        return
    lines = p.read_text(encoding="utf-8").splitlines()
    keep = lines[len(lines) // 2 :]
    tmp = p.with_suffix(".jsonl.tmp")
    tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
    tmp.replace(p)  # atomic


@dataclass(frozen=True)
class WaitSpan:
    """一段 🔴 等你：從轉 WAITING 到下一個狀態轉換（或現在，若還在等）。"""

    session_id: str
    project: str
    started: float
    seconds: float
    ongoing: bool  # 還在等（沒有後續轉換）


@dataclass
class ProjectStats:
    project: str
    waits: int = 0
    total_seconds: float = 0.0
    max_seconds: float = 0.0
    ongoing: int = 0

    @property
    def avg_seconds(self) -> float:
        return self.total_seconds / self.waits if self.waits else 0.0


def _read_events(path: Path) -> list[dict[str, object]]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    events = []
    for line in text.splitlines():
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and isinstance(data.get("ts"), (int, float)):
            events.append(data)
    return events


def collect_waits(since_seconds: float, *, path: Path | None = None, now: float | None = None) -> list[WaitSpan]:
    """從事件 log 撈出時間窗內開始的等待段（依 session 分組、時間排序後配對）。"""
    p = path or EVENTS_PATH
    t_now = now if now is not None else time.time()
    cutoff = t_now - since_seconds

    by_session: dict[str, list[dict[str, object]]] = {}
    for e in _read_events(p):
        by_session.setdefault(str(e.get("session_id", "")), []).append(e)

    spans: list[WaitSpan] = []
    for sid, events in by_session.items():
        events.sort(key=lambda e: float(e["ts"]))  # type: ignore[arg-type]
        for i, e in enumerate(events):
            if e.get("status") != "waiting":
                continue
            started = float(e["ts"])  # type: ignore[arg-type]
            if started < cutoff or started > t_now:
                continue
            nxt = events[i + 1] if i + 1 < len(events) else None
            ended = float(nxt["ts"]) if nxt is not None else t_now  # type: ignore[arg-type]
            cwd = str(e.get("cwd", ""))
            spans.append(
                WaitSpan(
                    session_id=sid,
                    project=Path(cwd).name or cwd or "—",
                    started=started,
                    seconds=max(0.0, ended - started),
                    ongoing=nxt is None,
                )
            )
    return spans


def aggregate(spans: list[WaitSpan]) -> list[ProjectStats]:
    """按專案彙整等待段，依總等待時間由長到短排序。"""
    by_project: dict[str, ProjectStats] = {}
    for span in spans:
        stat = by_project.setdefault(span.project, ProjectStats(project=span.project))
        stat.waits += 1
        stat.total_seconds += span.seconds
        stat.max_seconds = max(stat.max_seconds, span.seconds)
        if span.ongoing:
            stat.ongoing += 1
    return sorted(by_project.values(), key=lambda s: s.total_seconds, reverse=True)
