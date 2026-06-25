"""Notifier 協定 ＋ 所有後端共用的純 helper（只放契約與共用文字，不放具體後端）。

core 只透過 ``Notifier`` 介面跟具體平台溝通；要支援新平台＝在這個 package 裡新增一個
模組、定義一個 Notifier 並 ``register_notifier()`` 註冊，core 零改動。
"""

from __future__ import annotations

from typing import Protocol

from ring.registry import Session


class Notifier(Protocol):
    """一個系統通知後端。core 只透過這個介面跟具體平台溝通。"""

    name: str

    def available(self) -> bool:
        """這個後端在當前系統能不能用（通常是對應 binary 是否存在）。"""
        ...

    def supports_click(self) -> bool:
        """點擊通知能不能觸發 ``ring focus`` 跳轉。"""
        ...

    def send(self, sessions: list[Session]) -> None:
        """逐 session 各發一則通知；失敗安靜吞掉。"""
        ...


def notify_message(session: Session) -> str:
    """通知內文——只給重要訊息：去哪（完整路徑或 tmux 座標）。

    標題已經說了「哪個專案在等你」，內文就補「位置」讓你（尤其不能點擊跳轉時）知道去哪。
    """
    return f"📍 {session.location}"
