"""osascript 後端（macOS 純文字，點擊不可聚焦——terminal-notifier 被擋掉時的退路）。"""

from __future__ import annotations

from ring.config import get_config
from ring.i18n import gettext as _
from ring.notify.base import notify_message
from ring.notify.command import CommandNotifier
from ring.osascript import osascript
from ring.registry import Session


class OsascriptNotifier(CommandNotifier):
    name = "osascript"

    def supports_click(self) -> bool:
        return False

    def send(self, sessions: list[Session]) -> None:
        """用 osascript 逐 session 各發一則純文字通知（fallback，點擊不可聚焦）。"""
        cfg = get_config()
        for s in sessions:
            message = notify_message(s)
            title = _("RiNG · {project} 在等你回話", project=s.project)
            sound = f' sound name "{cfg.notify_sound_name}"' if cfg.notify_sound else ""
            try:
                osascript(f'display notification "{message}" with title "{title}"{sound}')
            except Exception:
                pass


notifier = OsascriptNotifier()
