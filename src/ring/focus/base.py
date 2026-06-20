"""Focuser 協定 ＋ 共用的「用 tty 聚焦 macOS 終端分頁」基底。

``Focuser.try_focus`` 的回傳語意：
  None          → 不歸我管（dispatcher 換下一個）
  (True, msg)   → 我接手且成功
  (False, err)  → 我接手但失敗（回報原因，不再往下試）
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Protocol

from ring.registry import Session


class Focuser(Protocol):
    name: str

    def try_focus(self, session: Session) -> tuple[bool, str] | None: ...


def osascript(script: str) -> tuple[int, str, str]:
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


class AppleScriptTTYFocuser:
    """用 session 的 tty 在某個 macOS 終端 app 裡找到對應分頁並聚焦。

    各 app 提供自己的 AppleScript（用 ``is running`` 守衛，沒在跑的 app 不會被喚醒）。
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
        return False, err or out or f"returncode={rc}"
