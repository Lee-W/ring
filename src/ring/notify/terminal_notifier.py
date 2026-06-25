"""terminal-notifier 後端（macOS，支援點擊 ``ring focus`` 跳轉）。"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ring.config import get_config
from ring.i18n import gettext as _
from ring.notify.base import notify_message
from ring.notify.command import CommandNotifier
from ring.registry import Session


def _ring_executable() -> str:
    """回傳可被 terminal-notifier click callback 執行的 ring 路徑。

    macOS 從通知中心觸發 ``-execute`` 時不一定有使用者 shell 的 PATH；通知建立時
    先解析成絕對路徑，點擊才不會因找不到 ``ring`` 而失效。
    """
    current = Path(sys.argv[0])
    if current.is_absolute() and current.exists():
        return str(current)
    found = shutil.which("ring")
    return found or "ring"


def _ring_focus_command(session_id: str) -> str:
    return f"{shlex.quote(_ring_executable())} focus {shlex.quote(session_id)}"


class TerminalNotifierNotifier(CommandNotifier):
    name = "terminal-notifier"

    def supports_click(self) -> bool:
        return True

    def send(self, sessions: list[Session]) -> None:
        """每個 session 各發一則 terminal-notifier 通知，帶點擊聚焦回呼。"""
        cfg = get_config()
        for s in sessions:
            cmd = [
                "terminal-notifier",
                "-title",
                _("RiNG · {project} 在等你回話", project=s.project),
                "-message",
                notify_message(s),
                "-execute",
                _ring_focus_command(s.session_id),
            ]
            if cfg.notify_sound:
                cmd.extend(["-sound", cfg.notify_sound_name or "default"])
            if cfg.notify_ignore_dnd:
                cmd.append("-ignoreDnD")
            try:
                subprocess.run(cmd, capture_output=True, timeout=10)
            except Exception:
                pass


notifier = TerminalNotifierNotifier()
