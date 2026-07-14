"""Claude Code：掃 ``~/.claude/projects`` 的 JSONL。"""

from __future__ import annotations

import time

import ring.registry as registry
from ring.registry import Session


def _activate_background_agents(sessions: list[Session], agent_ids: set[str]) -> None:
    """只依明確 session id 恢復仍有 process 的背景 agent，不用它們的 cwd 猜前景活性。"""
    now = time.time()
    for session in sessions:
        if session.session_id not in agent_ids:
            continue
        idle = now - session.last_active
        session.status = registry._scan_status(idle)
        session.status = registry._apply_waiting(
            session.status,
            idle,
            session._tail_kind,
            registry.WAITING_WINDOW_SECONDS,
        )


class ClaudeCodeSource:
    name = "claude-code"

    def discover(self) -> list[Session]:
        # 背景 agent 的 process cwd 可能仍是 daemon／啟動目錄，不能拿來按 cwd 猜哪些
        # 前景 transcript 活著。前景先只用可聚焦 process 判活，背景再按 session id 認領。
        procs = registry._claude_procs()
        if procs is None:
            # 這輪 ps/lsof 掃描失敗（未知）。掃描結果未知時，scan 只能把每個 transcript
            # 判成 ENDED；那批 ENDED row 會經由 _merge_duplicate_session 覆蓋掉 hook
            # 已保護好的 WAITING row，session 就從看板上消失。未知不等於離場——這輪
            # 不貢獻任何 row。
            return []
        agent_ids = registry.background_agent_session_ids() or set()
        merged: dict[str, Session] = {}
        scanned = registry._scan_sessions(procs)
        _activate_background_agents(scanned, agent_ids)
        for s in scanned:
            merged.setdefault(s.session_id, s)
        existing = list(merged.values())
        for s in registry._synthetic_sessions(procs, existing):
            merged.setdefault(s.session_id, s)  # 合成列只填無 row 的 cwd
        return list(merged.values())


source = ClaudeCodeSource()
