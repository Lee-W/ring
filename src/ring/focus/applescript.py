"""用 tty 在 macOS 終端 app 裡找到分頁並聚焦的共用 focuser 基底。

iTerm2 / Terminal.app 共用這套機制（各自提供 AppleScript）；tmux 不走這條、自有實作，
所以這段「共用實作」放自己的模組，而不是擠進 ``base``（契約）。
"""

from __future__ import annotations

import shutil

from ring.i18n import gettext as _
from ring.osascript import osascript
from ring.registry import Session


class AppleScriptTTYFocuser:
    """用 session 的 tty 在某個 macOS 終端 app 裡找到對應分頁並聚焦。

    各 app 提供自己的 AppleScript（用 ``is running`` 守衛，沒在跑的 app 不會被喚醒）。

    script 的回傳值三選一：``ok``（找到且已在前景）、``notfound``（不歸這個 app 管）、
    ``unraised``（找到分頁但視窗沒浮到前景——腳本自己驗過才回這個，見 iterm2 模組）。
    """

    def __init__(self, app: str, script: str) -> None:
        self.name = app
        self._script = script

    def try_focus(self, session: Session) -> tuple[bool, str] | None:
        tty = session.tty
        if not tty or not shutil.which("osascript"):
            return None
        rc, out, err = osascript(self._script.format(tty=tty))
        if rc == 0 and out == "ok":
            return True, f"{self.name} {tty}"
        if rc == 0 and out == "notfound":
            return None  # 這個 app 沒有這個 tty → 換下一個 focuser
        if rc == 0 and out == "unraised":
            # 分頁在，但視窗沒被帶到前景（多半在別的 Space 或全螢幕）。照實回失敗，
            # 不要讓 TUI 顯示「跳過去了」卻什麼都沒發生。
            return False, _("{app} 找到分頁了，但視窗沒浮到前景（可能在別的 Space 或全螢幕）", app=self.name)
        return False, err or out or f"returncode={rc}"
