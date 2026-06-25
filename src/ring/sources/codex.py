"""Codex CLI：讀 ``~/.codex/state_5.sqlite`` threads，並用 live ``codex`` process 配 tty。"""

from __future__ import annotations

import ring.registry as registry
from ring.registry import Session


class CodexSource:
    name = "codex"

    def discover(self) -> list[Session]:
        return registry._codex_threads(registry._codex_procs())


source = CodexSource()
