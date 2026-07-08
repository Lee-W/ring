"""RiNG 自有狀態檔清理。

只清 RiNG 寫在 ``~/.config/ring/`` 底下的檔案；不碰 Claude Code / Codex 的
transcript、SQLite state 或任何 provider 原始資料。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

from ring.ipc import _FOCUS_REQUEST_PATH, _PRESENCE_PATH, _PRESENCE_TTL, _REQUEST_TTL
from ring.registry import (
    _SESSION_START_SOURCES,
    RING_REGISTRY,
    Status,
    _hook_sessions,
    collect_provider_procs,
    hidden_sessions,
    prune_hidden_sessions,
)

DEFAULT_OLDER_THAN_SECONDS = 7 * 24 * 60 * 60


@dataclass(frozen=True)
class GcCandidate:
    path: Path
    reason: str


@dataclass(frozen=True)
class GcResult:
    candidates: list[GcCandidate]
    deleted: list[GcCandidate]
    errors: list[tuple[GcCandidate, str]]
    dry_run: bool
    hidden_stale: list[str] = field(default_factory=list)  # 已（或將）清掉的隱藏清單條目 id
    hidden_remaining: int = 0  # 清完之後，目前隱藏中的 session 數


def parse_duration(raw: str) -> float:
    """解析簡單 duration：``30s``、``10m``、``2h``、``7d``，無單位視為秒。"""
    text = raw.strip().lower()
    if not text:
        raise ValueError("empty duration")
    unit = text[-1]
    if unit in {"s", "m", "h", "d"}:
        number = text[:-1]
        factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    else:
        number = text
        factor = 1
    seconds = float(number) * factor
    if seconds < 0:
        raise ValueError("negative duration")
    return seconds


def collect_candidates(*, older_than: float, all_ended: bool = False, now: float | None = None) -> list[GcCandidate]:
    """收集可清理的 RiNG 自有狀態檔。"""
    current = time.time() if now is None else now
    candidates: dict[Path, GcCandidate] = {}

    for candidate in _registry_candidates(older_than=older_than, all_ended=all_ended, now=current):
        candidates[candidate.path] = candidate
    for candidate in _ipc_candidates(now=current):
        candidates[candidate.path] = candidate

    return sorted(candidates.values(), key=lambda c: str(c.path))


def run_gc(
    *, older_than: float = DEFAULT_OLDER_THAN_SECONDS, all_ended: bool = False, dry_run: bool = False
) -> GcResult:
    candidates = collect_candidates(older_than=older_than, all_ended=all_ended)
    now = time.time()
    hidden_now = hidden_sessions()
    # 隱藏清單本來就空時，不必為了「找不到就清」去掃全部 source（省掉一輪 process/檔案掃描）。
    known_ids = _known_session_ids() if hidden_now else set()
    hidden_stale_preview = {
        sid: hidden_at
        for sid, hidden_at in hidden_now.items()
        if sid not in known_ids or now - hidden_at >= older_than
    }

    if dry_run:
        return GcResult(
            candidates=candidates,
            deleted=[],
            errors=[],
            dry_run=True,
            hidden_stale=sorted(hidden_stale_preview),
            hidden_remaining=len(hidden_now) - len(hidden_stale_preview),
        )

    deleted: list[GcCandidate] = []
    errors: list[tuple[GcCandidate, str]] = []
    for candidate in candidates:
        try:
            candidate.path.unlink(missing_ok=True)
            deleted.append(candidate)
        except OSError as e:
            errors.append((candidate, str(e)))

    removed = prune_hidden_sessions(known_ids=known_ids, older_than=older_than, now=now) if hidden_stale_preview else {}
    return GcResult(
        candidates=candidates,
        deleted=deleted,
        errors=errors,
        dry_run=False,
        hidden_stale=sorted(removed),
        hidden_remaining=len(hidden_sessions()),
    )


def _known_session_ids() -> set[str]:
    """目前任何已註冊來源（不管有沒有被手動隱藏）找得到的 session id。

    只用來判斷隱藏清單裡的條目是不是「哪裡都找不到了」；不碰、不引用
    ``discover_sessions()`` 的 merge / tmux 配對邏輯。
    """
    from ring.sources import sources as _registered_sources

    ids: set[str] = set()
    for source in _registered_sources():
        for s in source.discover():
            ids.add(s.session_id)
    return ids


def _registry_candidates(*, older_than: float, all_ended: bool, now: float) -> list[GcCandidate]:
    if not RING_REGISTRY.is_dir():
        return []

    candidates: list[GcCandidate] = []
    sessions = _hook_sessions(
        procs_by_provider=collect_provider_procs(),
        purge_session_start_phantoms=False,
    )
    ended = {s.session_id: s for s in sessions if s.status is Status.ENDED}

    for path in RING_REGISTRY.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            candidates.append(GcCandidate(path, "registry JSON 無法讀取"))
            continue

        provider = str(data.get("provider", "claude-code") or "claude-code")
        if provider in _SESSION_START_SOURCES:
            candidates.append(GcCandidate(path, "舊版 SessionStart 幽靈檔"))
            continue

        sid = str(data.get("session_id", ""))
        session = ended.get(sid)
        if session is None:
            continue

        last_active = _last_active(data)
        if all_ended or now - last_active >= older_than:
            candidates.append(GcCandidate(path, "已離場 registry"))

    return candidates


def _ipc_candidates(*, now: float) -> list[GcCandidate]:
    candidates: list[GcCandidate] = []
    candidates.extend(_stale_json_file(_FOCUS_REQUEST_PATH, ttl=_REQUEST_TTL, now=now, label="focus-request"))
    candidates.extend(_stale_json_file(_PRESENCE_PATH, ttl=_PRESENCE_TTL, now=now, label="tui-presence"))
    return candidates


def _stale_json_file(path: Path, *, ttl: float, now: float, label: str) -> list[GcCandidate]:
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return []
    try:
        data = json.loads(raw)
        ts = float(data["ts"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return [GcCandidate(path, f"{label} JSON 無法讀取")]
    if now - ts > ttl:
        return [GcCandidate(path, f"{label} 已過期")]
    return []


def _last_active(data: object) -> float:
    if not isinstance(data, dict):
        return 0.0
    try:
        return float(data.get("last_active", 0.0))
    except (TypeError, ValueError):
        return 0.0
