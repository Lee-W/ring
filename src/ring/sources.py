"""Session 來源——讓 RiNG 不綁死 Claude Code。

每個工具是一個 ``SessionSource``：core 不認識任何具體工具，只把各 source 吐出的
``Session`` 收齊、配 tmux 座標、排序。要支援別的 agent CLI＝寫一個 source、
``register_source()`` 註冊，core 零改動（跟 ``focus.py`` 的 focuser 同一套設計）。

``Session`` 本身已是工具中立的（session_id / cwd / status / last_action / tty…），
所以新 source 只要負責「怎麼找到自己的 session、怎麼填這個 model」。
"""

from __future__ import annotations

from typing import Protocol

import ring.registry as registry
from ring.registry import Session


class SessionSource(Protocol):
    name: str

    def discover(self) -> list[Session]: ...


class ClaudeCodeSource:
    """Claude Code：掃 ``~/.claude/projects`` 的 JSONL ＋ 讀 hook registry。"""

    name = "claude-code"

    def discover(self) -> list[Session]:
        procs = registry._claude_procs()
        merged: dict[str, Session] = {s.session_id: s for s in registry._hook_sessions(procs)}
        for s in registry._scan_sessions(procs):
            merged.setdefault(s.session_id, s)  # 同一 session 以 hook 為準
        existing = list(merged.values())
        for s in registry._synthetic_sessions(procs, existing):
            merged.setdefault(s.session_id, s)  # 合成列只填無 row 的 cwd
        return list(merged.values())


# 註冊表（順序＝彙整順序）。外部要支援新工具就 register_source() 多塞一個。
_SOURCES: list[SessionSource] = [ClaudeCodeSource()]


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
            merged.setdefault(s.session_id, s)
    found = list(merged.values())
    targets = registry._tmux_targets()
    for s in found:
        s.tmux_target = targets.get(s.cwd)
    found.sort(key=lambda s: (s.status.rank, s.idle_for))
    return found
