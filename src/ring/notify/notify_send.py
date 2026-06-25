"""notify-send 後端（Linux / libnotify 純文字，點擊不可聚焦）。"""

from __future__ import annotations

import subprocess

from ring.i18n import gettext as _
from ring.notify.base import notify_message
from ring.notify.command import CommandNotifier
from ring.registry import Session


class NotifySendNotifier(CommandNotifier):
    name = "notify-send"

    def supports_click(self) -> bool:
        return False

    def send(self, sessions: list[Session]) -> None:
        """用 libnotify 的 ``notify-send`` 逐 session 各發一則純文字通知。"""
        for s in sessions:
            title = _("RiNG · {project} 在等你回話", project=s.project)
            message = notify_message(s)
            try:
                subprocess.run(["notify-send", title, message], capture_output=True, timeout=10)
            except Exception:
                pass


notifier = NotifySendNotifier()
