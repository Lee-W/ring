"""iTerm2 focuser（macOS）：用 tty 找到對應分頁並帶到前景。

跨視窗（不是同一視窗換分頁）跳轉踩過三個坑，腳本的形狀都是為了繞開它們（皆 2026-07-25 實測）：

1. **位置型 specifier 會在迴圈中途失效**。``repeat with w in windows`` 的迴圈變數其實是
   ``item M of every window`` 這種「按位置」的參照；一旦 windows 的順序在掃描途中變動
   （剛跳過一次視窗、使用者手動點了別的視窗都會），下一次取值就噴
   ``Invalid index (-1719)``，跳轉整個失敗——這正是「不同視窗有時抓不到」的主因。
   對策：掃描包在 ``try`` 裡、順序被動到就重掃；``select`` 之後不再用掃描時的位置，
   改以 ``current window`` 為根重找一次分頁。
2. **``select`` 是非同步的**：送出後立刻讀狀態會讀到舊值。所以最後隔 0.1 秒回頭驗
   ``current session of current window``，最多等 1 秒。驗不到就回 ``unraised``，讓上層
   照實說「沒帶到前景」（例如視窗在別的 Space），而不是假裝跳成功。
3. **不能用的兩招**：``set frontmost of w to true``（Terminal.app 那招）在 iTerm2 噴
   AppleEvent handler failed (-10000)；``set index of w to 1`` 會主動重排 windows，
   等於自己製造第 1 點的 -1719。

縮到 Dock 的視窗 ``select`` 就叫得回來（實測），``miniaturized`` 那段只是保險。
"""

from __future__ import annotations

from ring.focus.applescript import AppleScriptTTYFocuser

_SCRIPT = """
if application "iTerm2" is running then
  tell application "iTerm2"
    repeat 3 times
      try
        set wIdx to 0
        set tIdx to 0
        set sIdx to 0
        repeat with wi from 1 to (count of windows)
          repeat with ti from 1 to (count of tabs of window wi)
            repeat with si from 1 to (count of sessions of tab ti of window wi)
              if tty of session si of tab ti of window wi is "{tty}" then
                set wIdx to wi
                set tIdx to ti
                set sIdx to si
                exit repeat
              end if
            end repeat
            if wIdx is not 0 then exit repeat
          end repeat
          if wIdx is not 0 then exit repeat
        end repeat
        if wIdx is 0 then return "notfound"
        activate
        try
          if miniaturized of window wIdx then set miniaturized of window wIdx to false
        end try
        select window wIdx
        tell current window
          repeat with ti2 from 1 to (count of tabs)
            repeat with si2 from 1 to (count of sessions of tab ti2)
              if tty of session si2 of tab ti2 is "{tty}" then
                select tab ti2
                select session si2 of tab ti2
                exit repeat
              end if
            end repeat
          end repeat
        end tell
        repeat 10 times
          delay 0.1
          try
            if tty of current session of current window is "{tty}" then return "ok"
          end try
        end repeat
        return "unraised"
      on error number errNum
        if errNum is -128 then error number -128
        delay 0.2
      end try
    end repeat
    return "unraised"
  end tell
end if
return "notfound"
"""

focuser = AppleScriptTTYFocuser("iTerm2", _SCRIPT)
