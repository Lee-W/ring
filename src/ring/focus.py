"""把焦點跳到一個 session 實際所在的終端——可插拔、不綁特定 vendor。

每個終端是一個 ``Focuser``：core 不認識任何具體終端，只依序問每個 focuser
「這個 session 歸不歸你管」。要支援新終端＝加一個 focuser，core 零改動。

``Focuser.try_focus`` 的回傳語意：
  None          → 不歸我管（dispatcher 換下一個）
  (True, msg)   → 我接手且成功
  (False, err)  → 我接手但失敗（回報原因，不再往下試）
"""

from __future__ import annotations

import shutil
import subprocess
from typing import Protocol

from ring.config import get_config
from ring.i18n import gettext as _
from ring.registry import Session


class Focuser(Protocol):
    name: str

    def try_focus(self, session: Session) -> tuple[bool, str] | None: ...


def _osascript(script: str) -> tuple[int, str, str]:
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


class TmuxFocuser:
    name = "tmux"

    def try_focus(self, session: Session) -> tuple[bool, str] | None:
        target = session.tmux_target
        if not target or not shutil.which("tmux"):
            return None
        tmux_session = target.split(":", 1)[0]
        window = target.split(".", 1)[0]
        ok = False
        last_err = ""
        for cmd in (
            ["tmux", "switch-client", "-t", tmux_session],
            ["tmux", "select-window", "-t", window],
            ["tmux", "select-pane", "-t", target],
        ):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            except (OSError, subprocess.SubprocessError) as exc:
                return False, str(exc)
            if result.returncode == 0:
                ok = True
            else:
                last_err = result.stderr.strip()
        return (True, f"tmux {target}") if ok else (False, last_err or "tmux switch failed")


# 各 macOS 終端「用 tty 找到對應分頁並聚焦」的 AppleScript。
# 用 `is running` 守衛——若該 app 沒在跑就直接 notfound，不會被 AppleScript 喚醒。
_APPLESCRIPT_BY_TTY = {
    "iTerm2": """
if application "iTerm2" is running then
  tell application "iTerm2"
    repeat with w in windows
      repeat with t in tabs of w
        repeat with s in sessions of t
          if tty of s is "{tty}" then
            select w
            select t
            select s
            activate
            return "ok"
          end if
        end repeat
      end repeat
    end repeat
  end tell
end if
return "notfound"
""",
    "Terminal": """
if application "Terminal" is running then
  tell application "Terminal"
    repeat with w in windows
      repeat with t in tabs of w
        if tty of t is "{tty}" then
          set selected tab of w to t
          set frontmost of w to true
          activate
          return "ok"
        end if
      end repeat
    end repeat
  end tell
end if
return "notfound"
""",
}


class AppleScriptTTYFocuser:
    """用 session 的 tty 在某個 macOS 終端 app 裡找到對應分頁並聚焦。"""

    def __init__(self, app: str) -> None:
        self.name = app
        self._script = _APPLESCRIPT_BY_TTY[app]

    def try_focus(self, session: Session) -> tuple[bool, str] | None:
        tty = session.tty
        if not tty or not shutil.which("osascript"):
            return None
        rc, out, err = _osascript(self._script.format(tty=tty))
        if rc == 0 and out == "ok":
            return True, f"{self.name} {tty}"
        if rc == 0 and out == "notfound":
            return None  # 這個 app 沒有這個 tty → 換下一個 focuser
        return False, err or out or f"returncode={rc}"


# 內建 focuser。順序可由 config 的 `focusers` 覆寫；外部要加終端
# （Ghostty / Kitty / WezTerm…）寫個符合 Focuser 協定的類別，再 register_focuser() 即可。
_BUILTIN: dict[str, Focuser] = {
    "tmux": TmuxFocuser(),
    "iTerm2": AppleScriptTTYFocuser("iTerm2"),
    "Terminal": AppleScriptTTYFocuser("Terminal"),
}


def _initial_focusers() -> list[Focuser]:
    order = get_config().focusers
    if order:
        return [_BUILTIN[name] for name in order if name in _BUILTIN]
    return list(_BUILTIN.values())


_FOCUSERS: list[Focuser] = _initial_focusers()


def register_focuser(focuser: Focuser, *, first: bool = False) -> None:
    """外部擴充入口：註冊一個自訂 focuser（first=True 插到最前、優先嘗試）。"""
    if first:
        _FOCUSERS.insert(0, focuser)
    else:
        _FOCUSERS.append(focuser)


def focusers() -> list[Focuser]:
    """目前已註冊的 focuser，順序即嘗試順序。"""
    return list(_FOCUSERS)


def jump(session: Session) -> tuple[bool, str]:
    """依序問每個 focuser，誰先接手就用誰。回傳 (成功?, 訊息)。"""
    failures: list[str] = []
    for focuser in _FOCUSERS:
        result = focuser.try_focus(session)
        if result is None:
            continue
        ok, msg = result
        if ok:
            return True, msg
        failures.append(f"{focuser.name}: {msg}")
    if failures:
        return False, "; ".join(failures)
    return False, _("沒有 focuser 接得住（裝 hook，或一個專案只開一個 session 才測得到 tty）")
