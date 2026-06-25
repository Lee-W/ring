"""系統通知 package——把「新轉為等你」的 session 發給使用者。

可插拔的 ``Notifier`` 抽象層（跟 ``focus`` 的 focuser、``sources`` 的 source 同一套
設計）：core 不認識任何具體後端，只依序問每個已註冊 notifier「你能用嗎」。要支援新
平台＝在這個 package 加一個模組、``register_notifier()`` 註冊，core 零改動。內建：

- ``terminal-notifier``（macOS，支援點擊 ``ring focus`` 跳轉）
- ``osascript``（macOS，純文字，點擊不可聚焦——terminal-notifier 被系統擋掉時的退路）
- ``notify-send``（Linux / libnotify，純文字）

選哪個由 config ``notify_backend`` 決定：``"auto"`` = 第一個「可用」的（優先支援點擊的），
或指定某個 notifier 名稱強制使用。通知失敗一律安靜吞掉——通知是錦上添花，絕不打斷主流程。
外部 binary（terminal-notifier / notify-send）不進 pyproject dependencies。
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from ring.config import get_config
from ring.i18n import gettext as _
from ring.notify.base import Notifier
from ring.notify.notify_send import notifier as _notify_send
from ring.notify.osascript_notifier import notifier as _osascript
from ring.notify.terminal_notifier import notifier as _terminal_notifier
from ring.registry import Session

# 一次性安裝引導的 marker 檔路徑。
_HINT_MARKER: Path = Path.home() / ".config" / "ring" / ".tn-hint-shown"

# 內建後端。順序即 auto 的偏好順序（前面優先）。terminal-notifier 先於 osascript（macOS），
# notify-send 殿後（Linux）。register_notifier(first=True) 可插到最前。
_NOTIFIERS: list[Notifier] = [_terminal_notifier, _osascript, _notify_send]


def register_notifier(notifier: Notifier, *, first: bool = False) -> None:
    """外部擴充入口：註冊一個自訂通知後端（first=True 插到最前、優先嘗試）。"""
    if first:
        _NOTIFIERS.insert(0, notifier)
    else:
        _NOTIFIERS.append(notifier)


def notifiers() -> list[Notifier]:
    """目前已註冊的 notifier，順序即 auto 的偏好順序。"""
    return list(_NOTIFIERS)


def _select_notifier(backend: str) -> Notifier | None:
    """依 config 的 notify_backend 選一個可用 notifier。

    - ``"none"`` → ``None``，明確關閉通知（RiNG 當純看板用，不發任何 toast）。
    - ``"agent-hooks"`` → 決策與提醒交給 agent-hooks（由 ``ring hook`` 同步出 modal）。
      agent-hooks 在 PATH 上時回 ``None``（watch 不重複發 toast）；不在時退回 auto，
      讓 RiNG 自己通知——所以「沒裝 agent-hooks 也不會兩頭落空」是自動保證的。
    - 指定名稱且該 notifier 可用 → 用它。
    - ``"auto"``（或指定的後端不存在/不可用）→ 取第一個可用的，優先支援點擊跳轉的。
    - 都不可用 → ``None``（不發、不崩）。
    """
    if backend == "none":
        return None
    if backend == "agent-hooks":
        if shutil.which("agent-hooks") is not None:
            return None  # agent-hooks 從 ring hook 那邊出 modal，watch 不重複發
        backend = "auto"  # 沒裝 → 退回 auto，RiNG 自己通知，不會瞎
    available = [n for n in _NOTIFIERS if n.available()]
    if backend != "auto":
        for n in available:
            if n.name == backend:
                return n
        # 指定的後端不可用 → 退回 auto 選法
    if not available:
        return None
    clickable = [n for n in available if n.supports_click()]
    return clickable[0] if clickable else available[0]


def notify_waiting(sessions: list[Session]) -> str | None:
    """對一批「新轉為等你」的 session 發系統通知。

    依 config ``notify_backend`` 選一個 notifier 後端發送（見 ``_select_notifier``）。
    auto 模式下若只選到不支援點擊的後端、且在 macOS 上，回傳一次性的安裝引導字串
    （建議裝 terminal-notifier 取得點擊跳轉）；其餘情況回 ``None``。失敗一律安靜吞掉。

    :param sessions: 新轉為 waiting 的 session 清單；空清單時直接回傳。
    :returns: 安裝引導提示字串或 ``None``。
    """
    if not sessions:
        return None

    backend = get_config().notify_backend
    notifier = _select_notifier(backend)
    if notifier is None:
        return None
    notifier.send(sessions)
    # auto 落到非點擊後端、且在 macOS（terminal-notifier 是可裝的點擊選項）→ 提示一次。
    if backend == "auto" and not notifier.supports_click() and sys.platform == "darwin":
        return _maybe_show_install_hint()
    return None


def _maybe_show_install_hint() -> str | None:
    """首次走 osascript 路徑時，回傳 terminal-notifier 安裝建議字串（marker 檔防重複）。

    :returns: hint 字串（首次）或 ``None``（已提示過或寫 marker 失敗時）。
    """
    if _HINT_MARKER.exists():
        return None
    try:
        hint = _("💡 裝 terminal-notifier 可點擊通知直接跳回 session：brew install terminal-notifier")
        _HINT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _HINT_MARKER.touch()
        return hint
    except Exception:
        return None


__all__ = ["Notifier", "notifiers", "notify_waiting", "register_notifier"]
