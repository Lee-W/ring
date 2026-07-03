"""Focuser 協定——core 跟具體終端之間的契約（只放 Protocol，不放具體實作）。

``try_focus`` 的回傳語意：
  None          → 不歸我管（dispatcher 換下一個）
  (True, msg)   → 我接手且成功
  (False, err)  → 我接手但失敗（記下原因，再試其他 focuser）

一般 focuser 成功後 dispatcher 就停止；需要先處理內層、再交給外層 focuser 的實作可設
``continue_after_success = True``（例如 Neovim 先切 terminal buffer，再讓 tmux 聚焦 pane）。
"""

from __future__ import annotations

from typing import Protocol

from ring.registry import Session


class Focuser(Protocol):
    name: str

    def try_focus(self, session: Session) -> tuple[bool, str] | None: ...
