"""agent CLI「自己」的桌面通知通道診斷（目前只有 Claude Code 有這種東西）。

為什麼需要這個模組：``ring install-hooks`` 只會寫 ``~/.claude/settings.json`` 的
``hooks`` 這一個 key（見 ``ring.hook._hook_targets``），從來不碰 Claude Code 自己的
``preferredNotifChannel``。這是兩條平行的路——hook 決定「RiNG 知不知道發生了什麼」，
``preferredNotifChannel`` 決定「Claude Code 自己要不要跳桌面通知」。裝了 hook 不會關掉
後者，所以同一個權限請求會有兩則通知；而且 Claude Code 那條還會在 Stop 之後補一則
``idle_prompt``（「閒著、換你了」），那是 RiNG 刻意判 🟡 不發的類別——使用者跳過去只會
看到一個空 prompt，沒有東西可回。

這個模組只負責把這個重複來源攤開給 ``ring doctor`` 看，**全程唯讀**：讀 settings 檔、看
環境變數，不改任何設定、不寫任何檔。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

CLAUDE_SETTINGS_PATH = Path.home() / ".claude" / "settings.json"

# Claude Code 的通知通道設定 key，以及沒設定時的值。
_CHANNEL_KEY = "preferredNotifChannel"
DEFAULT_CHANNEL = "auto"

# 通道值 → 會不會跳桌面通知。取自 claude binary 裡的通道 enum：
#   auto | iterm2 | terminal_bell | iterm2_with_bell | kitty | ghostty | notifications_disabled
# 這份對應只涵蓋「明確指定」的值；``auto`` 要看終端，另外由 _auto_kind() 判。
_EXPLICIT_KINDS = {
    "notifications_disabled": "off",
    "terminal_bell": "bell",
    "iterm2": "banner",
    "iterm2_with_bell": "banner",
    "kitty": "banner",
    "ghostty": "banner",
}

# ``auto`` 在這些終端底下有對應的專屬通道可挑 → 會跳桌面通知。
# 依據是上面那份 enum 裡有 iterm2 / kitty / ghostty 三個終端專屬值；auto 實際怎麼判
# 沒有公開文件，所以其他終端一律回 "unknown"，不假裝知道。
_AUTO_BANNER_TERMINALS = {"iTerm2", "kitty", "ghostty"}


@dataclass(frozen=True)
class NativeNotifyStatus:
    """Claude Code 自帶通知通道的現況（唯讀快照）。

    :param path: 讀的 settings 檔路徑。
    :param exists: 該檔存不存在（不存在 → Claude Code 用內建預設）。
    :param channel: 生效中的通道值；沒設定就是 ``DEFAULT_CHANNEL``。
    :param explicit: 通道是不是使用者明確設定的（False = 沒設、吃預設）。
    :param terminal: 偵測到的終端名；認不出來是空字串。
    :param kind: ``"off"`` / ``"bell"`` / ``"banner"`` / ``"unknown"``。
    """

    path: Path
    exists: bool
    channel: str
    explicit: bool
    terminal: str
    kind: str

    @property
    def duplicates_ring(self) -> bool:
        """這個設定會不會跟 RiNG 的通知重複（只有明確關掉才不會）。"""
        return self.kind != "off"


def detect_terminal(env: Mapping[str, str] | None = None) -> str:
    """從環境變數認出目前的終端；認不出回空字串。

    kitty 的標記（``KITTY_WINDOW_ID`` / ``TERM=xterm-kitty``）先看，因為 kitty 底下
    ``TERM_PROGRAM`` 不一定有值。認不出來的 ``TERM_PROGRAM`` 原樣回傳，讓 doctor 至少
    印得出「我看到的是什麼」。

    注意：tmux / ssh 裡 ``TERM_PROGRAM`` 可能不見，或被繼承成外層終端的值——所以這個
    結果只能當提示，不能當判定依據（呼叫端請照這個前提呈現）。
    """
    env = os.environ if env is None else env
    if env.get("KITTY_WINDOW_ID") or env.get("TERM", "") == "xterm-kitty":
        return "kitty"
    known = {
        "iTerm.app": "iTerm2",
        "Apple_Terminal": "Terminal",
        "ghostty": "ghostty",
        "WezTerm": "WezTerm",
        "vscode": "VS Code",
    }
    term_program = env.get("TERM_PROGRAM", "").strip()
    if not term_program:
        return ""
    return known.get(term_program, term_program)


def _read_channel(path: Path) -> tuple[bool, str, bool]:
    """讀 settings 檔的通道設定 → ``(檔案存在, 通道值, 是否明確設定)``。

    讀不到 / 不是合法 JSON / 值不是字串 → 一律當「沒設定」（吃預設），不拋例外：
    doctor 是唯讀診斷，任何一節壞掉都不該讓整份報告掛掉。
    """
    if not path.exists():
        return False, DEFAULT_CHANNEL, False
    try:
        data = json.loads(path.read_text() or "{}")
    except Exception:
        return True, DEFAULT_CHANNEL, False
    if not isinstance(data, dict):
        return True, DEFAULT_CHANNEL, False
    value = data.get(_CHANNEL_KEY)
    if not isinstance(value, str) or not value.strip():
        return True, DEFAULT_CHANNEL, False
    return True, value.strip(), True


def _auto_kind(terminal: str) -> str:
    """``auto`` 在這個終端底下會不會跳桌面通知。認不出的終端回 ``"unknown"``。"""
    return "banner" if terminal in _AUTO_BANNER_TERMINALS else "unknown"


def native_notify_status(path: Path | None = None, env: Mapping[str, str] | None = None) -> NativeNotifyStatus:
    """組出 Claude Code 自帶通知通道的現況快照。唯讀，不改任何設定。"""
    target = CLAUDE_SETTINGS_PATH if path is None else path
    exists, channel, explicit = _read_channel(target)
    terminal = detect_terminal(env)
    # 明確寫 "auto" 跟完全沒設定是同一件事，都要看終端才知道會不會跳通知。
    kind = _auto_kind(terminal) if channel == DEFAULT_CHANNEL else _EXPLICIT_KINDS.get(channel, "unknown")
    return NativeNotifyStatus(
        path=target,
        exists=exists,
        channel=channel,
        explicit=explicit,
        terminal=terminal,
        kind=kind,
    )


__all__ = ["CLAUDE_SETTINGS_PATH", "DEFAULT_CHANNEL", "NativeNotifyStatus", "detect_terminal", "native_notify_status"]
