"""Codex CLI：讀 ``~/.codex/state_5.sqlite`` threads，並用 live ``codex`` process 配 tty。"""

from __future__ import annotations

import ring.registry as registry
from ring.registry import Session


class CodexSource:
    name = "codex"

    def discover(self) -> list[Session]:
        procs = registry._codex_procs()
        if procs is None:
            # 這輪 ps 掃描失敗（未知）。拿空清單去掃會把每個 thread 判成 ENDED，
            # 覆蓋掉 hook 已保護的 row。未知不等於離場——這輪不貢獻任何 row。
            return []
        return registry._codex_threads(procs)


source = CodexSource()
