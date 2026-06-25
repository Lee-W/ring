"""Claude Code：掃 ``~/.claude/projects`` 的 JSONL。"""

from __future__ import annotations

import ring.registry as registry
from ring.registry import Session


class ClaudeCodeSource:
    name = "claude-code"

    def discover(self) -> list[Session]:
        procs = registry._claude_procs()
        merged: dict[str, Session] = {}
        for s in registry._scan_sessions(procs):
            merged.setdefault(s.session_id, s)
        existing = list(merged.values())
        for s in registry._synthetic_sessions(procs, existing):
            merged.setdefault(s.session_id, s)  # 合成列只填無 row 的 cwd
        return list(merged.values())


source = ClaudeCodeSource()
