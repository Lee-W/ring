"""iTerm2 focuser（macOS）：用 tty 找到對應分頁並帶到前景。"""

from __future__ import annotations

from ring.focus.applescript import AppleScriptTTYFocuser

_SCRIPT = """
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
"""

focuser = AppleScriptTTYFocuser("iTerm2", _SCRIPT)
