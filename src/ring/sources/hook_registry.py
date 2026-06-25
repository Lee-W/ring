"""RiNG hook registry：所有 provider 的精準事件來源。"""

from __future__ import annotations

import ring.registry as registry
from ring.registry import Session


class HookRegistrySource:
    name = "hook"

    def discover(self) -> list[Session]:
        return registry._hook_sessions(procs_by_provider=registry.collect_provider_procs())


source = HookRegistrySource()
