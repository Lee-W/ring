"""osascript 後端（macOS 純文字，點擊不可聚焦——terminal-notifier 被擋掉時的退路）。"""

from __future__ import annotations

from ring.config import get_config
from ring.notify.base import notify_message, notify_title
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
            title = notify_title(s)
            sound = f' sound name "{cfg.notify_sound_name}"' if cfg.notify_sound else ""
            try:
                osascript(f'display notification "{message}" with title "{title}"{sound}')
            except Exception:
                pass


notifier = OsascriptNotifier()
