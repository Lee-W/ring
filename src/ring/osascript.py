"""跑一段 AppleScript 的小工具——平台中立的共用 helper。

focus（找終端分頁）、notify（osascript 後端）、cli（doctor 探 app 是否在跑）都用它，
所以放在不綁任何 feature 的中性模組，而不是塞在 ``focus`` 裡讓別人反向依賴 focus。
"""

from __future__ import annotations

import subprocess


def osascript(script: str) -> tuple[int, str, str]:
    try:
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()
