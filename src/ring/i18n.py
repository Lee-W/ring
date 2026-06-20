"""RiNG i18n — gettext。

msgid 用**台灣漢語**：原始碼即預設語言，所以 zh-Hant 不需要任何 ``.mo``。
其他語言放 ``locale/<lang>/LC_MESSAGES/ring.mo``（由 ``.po`` 編譯，見 README / ``poe i18n-compile``）。

切語言：process 啟動時 ``set_lang()`` 裝上對應 translation；之後 ``_()`` / ``ngettext()`` 就用它。
在 ``import ring.tui`` 之前先 set_lang，class-level 的 Footer 按鍵說明也會跟著走。

``_()`` 與 ``ngettext()`` 都吃 kwargs：``_("…{x}…", x=1)`` 等同 gettext 後再 ``.format(x=1)``。
"""

from __future__ import annotations

import gettext as _gettext
import os
from pathlib import Path

_LOCALE_DIR = Path(__file__).parent / "locale"
_DOMAIN = "ring"

_current: _gettext.NullTranslations = _gettext.NullTranslations()


def resolve_lang(explicit: str | None = None) -> str:
    raw = (explicit or os.environ.get("RING_LANG") or os.environ.get("LANG") or "").lower()
    return "en" if raw.startswith("en") else "zh-Hant"


def set_lang(lang: str | None) -> None:
    """裝上對應語言的 translation。zh-Hant＝msgid 本身（不需 .mo）。"""
    global _current
    if resolve_lang(lang) == "zh-Hant":
        _current = _gettext.NullTranslations()
        return
    try:
        _current = _gettext.translation(_DOMAIN, _LOCALE_DIR, languages=[resolve_lang(lang)])
    except OSError:  # .mo 不在 → 安靜退回 msgid（中文）
        _current = _gettext.NullTranslations()


def gettext(msgid: str, /, **kw: object) -> str:
    # msgid 設 positional-only，這樣 format kwarg 取任何名字（msg / n …）都不會撞參數。
    out = _current.gettext(msgid)
    return out.format(**kw) if kw else out


def ngettext(singular: str, plural: str, count: int, /, **kw: object) -> str:
    out = _current.ngettext(singular, plural, count)
    return out.format(**kw) if kw else out


_ = gettext
