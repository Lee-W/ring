"""SessionSource 協定（跟 ``focus/base``、``notify/base`` 同一套設計）。

``Session`` 本身已是工具中立的（session_id / cwd / status / last_action / tty…），
所以新 source 只要負責「怎麼找到自己的 session、怎麼填這個 model」。
"""

from __future__ import annotations

from typing import Protocol

from ring.registry import Session


class SessionSource(Protocol):
    name: str

    def discover(self) -> list[Session]: ...
