"""外部 plugin 載入——讓「可插拔」對裝好的 `ring` 指令也成立。

``register_source()`` / ``register_focuser()`` / ``register_notifier()`` 只在同一個
process 內有效；第三方套件要讓 ``ring`` CLI 吃到自己的後端，得有人替它把註冊碼跑起來。
這裡提供兩條路，CLI 啟動時各掃一次：

1. **entry point**（發佈套件用）：在對方的 ``pyproject.toml`` 宣告::

       [project.entry-points."ring.plugins"]
       mytool = "ring_mytool.plugin"

   值指向一個模組或 callable；模組在 import 時自行呼叫 ``register_*()``，
   callable 則會被無參數呼叫一次。

2. **config**（本機腳本用）：``~/.config/ring/config.toml`` 寫::

       plugins = ["my_local_module"]

   模組須在 ``sys.path`` 上（例如放 site-packages 或 PYTHONPATH），import 時自行註冊。

單一 plugin 壞掉只警告（stderr 一行）、不擋主流程；整個載入器 idempotent，
同 process 內重複呼叫不會重複註冊。
"""

from __future__ import annotations

import sys
from importlib import import_module
from importlib.metadata import entry_points

from ring.config import get_config
from ring.i18n import gettext as _

ENTRY_POINT_GROUP = "ring.plugins"

_loaded = False


def load_plugins() -> list[str]:
    """掃 entry points 與 config 的 plugins 清單，各載入一次；回傳成功載入的名字。

    重複呼叫直接回空清單（不重複註冊）。失敗的 plugin 印一行警告到 stderr 後跳過。
    """
    global _loaded
    if _loaded:
        return []
    _loaded = True

    loaded: list[str] = []
    try:
        eps = list(entry_points(group=ENTRY_POINT_GROUP))
    except Exception:
        eps = []
    for ep in eps:
        try:
            obj = ep.load()
            if callable(obj):
                obj()
            loaded.append(ep.name)
        except Exception as e:  # plugin 壞掉不擋看板
            print(_("⚠️ ring plugin '{name}' 載入失敗：{error}", name=ep.name, error=e), file=sys.stderr)

    for mod in get_config().plugins:
        try:
            import_module(mod)
            loaded.append(mod)
        except Exception as e:
            print(_("⚠️ ring plugin '{name}' 載入失敗：{error}", name=mod, error=e), file=sys.stderr)
    return loaded


def _reset_for_tests() -> None:
    """測試隔離用：清掉 idempotent 旗標。"""
    global _loaded
    _loaded = False
