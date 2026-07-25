"""跳轉取證 log——每次 ``jump()`` append 一行到 ``~/.config/ring/focus.jsonl``。

為什麼要記：跳轉失敗是間歇性的，而 TUI 的 toast 看過就沒了，事後只剩「昨天有一次跳不
過去」這種無法歸因的敘述。這份 log 把每個 focuser 當下的回覆存下來，讓下一次重現時能
直接分辨是哪一種失敗：

- 每個 focuser 都回 ``skip``、而 session 有 tty → tty 已失效（分頁關了／registry 記的是舊的）
- ``tty`` 是空的 → 根本沒抓到 tty，focuser 沒得比對
- iTerm2 回 ``unraised`` → 分頁找到了但視窗沒浮上來（多半在別的 Space）
- iTerm2 回 osascript 錯誤 → 權限或 5 秒 timeout

跳轉是使用者手動觸發、頻率低（一行約 200 bytes），所以預設就開著、不做開關——要能在
「下次發生」時就有證據，而不是先想起來去開 debug 旗標。寫入失敗一律安靜吞掉。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

FOCUS_LOG_PATH = Path.home() / ".config" / "ring" / "focus.jsonl"

# 一行約 200 bytes，1MB ≈ 5000 次跳轉。超過就砍半保新。
_MAX_BYTES = 1024 * 1024


def log_jump(
    *,
    session_id: str,
    provider: str,
    tty: str,
    tmux_target: str,
    attempts: list[tuple[str, str]],
    ok: bool,
    msg: str,
    path: Path | None = None,
    now: float | None = None,
) -> None:
    """append 一次跳轉的完整經過；失敗安靜吞掉（呼叫端不需要自己包 try）。

    :param attempts: ``[(focuser 名稱, 結果), …]``，結果為 ``skip`` / ``ok`` / ``fail: …``。
    """
    p = path or FOCUS_LOG_PATH
    try:
        line = json.dumps(
            {
                "ts": now if now is not None else time.time(),
                "session_id": session_id,
                "provider": provider,
                "tty": tty,
                "tmux_target": tmux_target,
                "attempts": [{"focuser": name, "result": result} for name, result in attempts],
                "ok": ok,
                "msg": msg,
            },
            ensure_ascii=False,
            default=str,
        )
        p.parent.mkdir(parents=True, exist_ok=True)
        _trim_if_oversized(p)
        with p.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def _trim_if_oversized(p: Path) -> None:
    """log 超過 ``_MAX_BYTES`` 時砍半保新，避免無上限成長。失敗安靜放棄（不擋 append）。"""
    try:
        if p.stat().st_size <= _MAX_BYTES:
            return
        lines = p.read_text(encoding="utf-8").splitlines()
        keep = lines[len(lines) // 2 :]
        tmp = p.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(keep) + "\n", encoding="utf-8")
        tmp.replace(p)  # atomic
    except Exception:
        pass
