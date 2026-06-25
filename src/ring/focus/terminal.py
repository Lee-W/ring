"""Terminal.app focuser（macOS）：用 tty 找到對應分頁並帶到前景。"""

from __future__ import annotations

from ring.focus.applescript import AppleScriptTTYFocuser

_SCRIPT = """
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
"""

focuser = AppleScriptTTYFocuser("Terminal", _SCRIPT)
