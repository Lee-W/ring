"""Focuser 協定——core 跟具體終端之間的契約（只放 Protocol，不放具體實作）。

``try_focus`` 的回傳語意：
  None          → 不歸我管（dispatcher 換下一個）
  (True, msg)   → 我接手且成功
  (False, err)  → 我接手但失敗（回報原因，不再往下試）
"""

from __future__ import annotations

from typing import Protocol

from ring.registry import Session


class Focuser(Protocol):
    name: str

    def try_focus(self, session: Session) -> tuple[bool, str] | None: ...
