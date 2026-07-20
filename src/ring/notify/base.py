"""Notifier 協定 ＋ 所有後端共用的純 helper（只放契約與共用文字，不放具體後端）。

core 只透過 ``Notifier`` 介面跟具體平台溝通；要支援新平台＝在這個 package 裡新增一個
模組、定義一個 Notifier 並 ``register_notifier()`` 註冊，core 零改動。
"""

from __future__ import annotations

from typing import Protocol

from ring.i18n import gettext as _
from ring.labels import get_label
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


def display_name(session: Session) -> str:
    """通知顯示用名稱：使用者取過名（TUI 按 ``n``）就用名字，否則用專案（目錄）名。"""
    return get_label(session.session_id) or session.project


def is_summary_session(session: Session) -> bool:
    """這個 Session 是不是彙總通知用的標記（見 ``Session.is_summary`` / ``ring.notify.notify_summary``）。

    彙總只需要「發一則」而非逐 session 發，靠這個 flag 讓 notify_title / notify_message
    改走彙總句式，不必碰任何一個後端模組的 send() 實作——各後端仍是呼叫同一個
    send(list[Session])，只是那個 Session 的 ``is_summary`` 剛好是 True。``session_id``
    本身維持真實值（借用某個真的被合流的 session），點擊這則通知能正確 focus 過去，
    不會像 sentinel id 那樣找不到 session。
    """
    return session.is_summary


def notify_title(session: Session) -> str:
    """通知標題——「哪個 session 在等你」，全後端共用一句。彙總通知改用固定句。"""
    if is_summary_session(session):
        return _("RiNG · 還有人在等你")
    return _("RiNG · {project} 在等你回話", project=display_name(session))


def notify_message(session: Session) -> str:
    """通知內文——它在等什麼（hook 有給 detail 時）＋去哪（完整路徑或 tmux 座標）。

    標題已經說了「誰在等你」，內文就補「等什麼、去哪」，讓你看一眼就能決定要不要現在回去。
    彙總通知（sentinel session）直接用 ``waiting_detail`` 存的彙總句，不附位置。
    """
    if is_summary_session(session):
        return session.waiting_detail
    location = f"📍 {session.location}"
    return f"{session.waiting_detail}\n{location}" if session.waiting_detail else location
