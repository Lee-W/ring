"""把焦點跳到 session 所在的終端——可插拔、不綁特定 vendor。

每個終端是一個 ``Focuser``（見 ``base.Focuser``）：core 不認識任何具體終端，
只依序問每個 focuser「這個 session 歸不歸你管」。要支援新終端＝寫一個模組、
``register_focuser()`` 註冊，core 零改動。內建：tmux / iTerm2 / Terminal.app /
Linux X11 視窗（wmctrl，best-effort fallback）。
"""

from __future__ import annotations

from ring.config import get_config
from ring.focus.base import Focuser
from ring.focus.iterm2 import focuser as _iterm2
from ring.focus.linux_wm import focuser as _linux_wm
from ring.focus.terminal import focuser as _terminal
from ring.focus.tmux import focuser as _tmux
from ring.i18n import gettext as _
from ring.registry import Session, Status

# 內建 focuser。順序可由 config 的 `focusers` 覆寫。tmux 跨平台、最快，排最前；
# macOS app 在非 macOS 會自己 return None；linux-wm 殿後當 X11 fallback。
_BUILTIN: dict[str, Focuser] = {
    "tmux": _tmux,
    "iTerm2": _iterm2,
    "Terminal": _terminal,
    "linux-wm": _linux_wm,
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
    if session.status is Status.ENDED:
        return False, _("已離場的 session 無法跳轉")
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
    if session.tty:
        return False, _("找不到這個終端分頁（可能已關閉；刷新後若仍存在，請裝 hook 取得更精準狀態）")
    return False, _("沒有 focuser 接得住（裝 hook，或一個專案只開一個 session 才測得到 tty）")


__all__ = ["Focuser", "focusers", "jump", "register_focuser"]
