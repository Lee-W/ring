"""以「對應 CLI binary 在不在 PATH」判定 available 的 notifier 共用基底。

內建三個後端（terminal-notifier / osascript / notify-send）的 binary 名剛好等於 notifier
名，所以共用這段；binary 名與 notifier 名不同的後端覆寫 ``available()`` 即可。子類別仍要
自己定義 ``name`` 與 ``supports_click()`` / ``send()``——跟 ``focus.applescript`` 把共用實作
拆出 ``base`` 同一個道理。
"""

from __future__ import annotations

import shutil


class CommandNotifier:
    name: str

    def available(self) -> bool:
        return shutil.which(self.name) is not None
