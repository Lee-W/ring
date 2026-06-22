"""系統通知模組——把「新轉為等你」的 session 發給使用者。

優先使用 terminal-notifier（brew 安裝的外部 binary），帶點擊回呼
``ring focus <session_id>`` 讓使用者點擊後直接聚焦到對應終端。
未裝 terminal-notifier 則退化為 osascript 純文字通知（點擊不可聚焦）。

通知失敗一律安靜吞掉——通知是錦上添花，絕不打斷主流程。
terminal-notifier 不進 pyproject dependencies（brew 外部 binary）。
"""

from __future__ import annotations

import shutil
import subprocess

from ring.focus.base import osascript
from ring.registry import Session


def notify_waiting(sessions: list[Session]) -> None:
    """對一批「新轉為等你」的 session 發系統通知。

    - 有 terminal-notifier → 每筆各發一則，帶 ``-execute "ring focus <session_id>"``。
    - 無 terminal-notifier → fallback osascript display notification（純文字）。
    - 失敗一律安靜吞掉，不拋例外。

    :param sessions: 新轉為 waiting 的 session 清單；空清單時直接回傳。
    """
    if not sessions:
        return

    if shutil.which("terminal-notifier"):
        _notify_with_terminal_notifier(sessions)
    else:
        _notify_with_osascript(sessions)


def _notify_with_terminal_notifier(sessions: list[Session]) -> None:
    """每個 session 各發一則 terminal-notifier 通知，帶點擊聚焦回呼。"""
    for s in sessions:
        try:
            subprocess.run(
                [
                    "terminal-notifier",
                    "-title",
                    "RiNG",
                    "-message",
                    f"{s.project} 在等你回話",
                    "-execute",
                    f"ring focus {s.session_id}",
                ],
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass


def _notify_with_osascript(sessions: list[Session]) -> None:
    """用 osascript 發一則純文字通知（fallback，點擊不可聚焦）。"""
    names = ", ".join(s.project for s in sessions)
    try:
        osascript(f'display notification "{names} 在等你回話" with title "RiNG"')
    except Exception:
        pass
