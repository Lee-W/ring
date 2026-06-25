"""Session 來源 package——讓 RiNG 不綁死 Claude Code。

每個工具是一個 ``SessionSource``：core 不認識任何具體工具，只把各 source 吐出的
``Session`` 收齊、配 tmux 座標、排序。要支援別的 agent CLI＝在這個 package 加一個
模組、``register_source()`` 註冊，core 零改動（跟 ``focus`` 的 focuser 同一套設計）。
"""

from __future__ import annotations

import ring.registry as registry
from ring.registry import Session, Status
from ring.sources.base import SessionSource
from ring.sources.claude_code import source as _claude_code
from ring.sources.codex import source as _codex
from ring.sources.hook_registry import source as _hook_registry

# 註冊表（順序＝彙整順序）。hook registry 先於 zero-config source，精準事件優先。
_SOURCES: list[SessionSource] = [_hook_registry, _claude_code, _codex]


def register_source(source: SessionSource, *, first: bool = False) -> None:
    """外部擴充入口：註冊一個自訂 session 來源。"""
    if first:
        _SOURCES.insert(0, source)
    else:
        _SOURCES.append(source)


def sources() -> list[SessionSource]:
    """目前已註冊的來源。"""
    return list(_SOURCES)


def discover_sessions() -> list[Session]:
    """場館點名：彙整所有來源的 session，配 tmux 座標、排序（等你的排最上面）。"""
    merged: dict[str, Session] = {}
    for source in _SOURCES:
        for s in source.discover():
            current = merged.get(s.session_id)
            merged[s.session_id] = s if current is None else _merge_duplicate_session(current, s)
    found = list(merged.values())
    targets = registry._tmux_targets()
    for s in found:
        s.tmux_target = targets.get(s.cwd)
    found.sort(key=lambda s: (s.status.rank, s.idle_for))
    return found


def _merge_duplicate_session(current: Session, candidate: Session) -> Session:
    """同一 session 來自多個來源時合併。

    hook registry 通常最精準，所以預設保留先到的 hook row。不過 Claude Code 有些
    action-required 狀態之後未必會送出能清掉 waiting 的 hook；如果 zero-config scan
    已看到同一 session 有更新紀錄且不再是 WAITING，就用 scan 清掉 stale waiting，
    同時保留 hook 拿到的 tty，避免跳轉能力退化。
    """
    if (
        current.source == "hook"
        and current.status is Status.WAITING
        and candidate.provider == current.provider
        and candidate.status is not Status.WAITING
        and candidate.last_active > current.last_active
    ):
        if not candidate.tty:
            candidate.tty = current.tty
        if not candidate.tmux_target:
            candidate.tmux_target = current.tmux_target
        return candidate
    return current


def get_by_id(session_id: str) -> Session | None:
    """現查現給：重跑 discover_sessions() 後 filter 出指定 session_id。

    每次呼叫都重跑 discover，不快取舊 Session——scan 的 tty 只在「該 cwd 剛好一個
    live claude」才填，舊 Session 的 tty 可能已失效（例如點擊通知時），
    必須重新 discover 才能拿到當下有效的 tty。

    :param session_id: 要查詢的 session id。
    :returns: 找到時回對應 Session；找不到回 None。
    """
    for s in discover_sessions():
        if s.session_id == session_id:
            return s
    return None


__all__ = [
    "SessionSource",
    "discover_sessions",
    "get_by_id",
    "register_source",
    "sources",
]
