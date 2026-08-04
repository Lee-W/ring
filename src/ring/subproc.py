"""跑外部指令時共用的小設定。

``registry`` 與 ``tmux_scan`` 都會在同一次看板刷新裡重複問 ``ps`` / ``tmux``，兩邊用
同一個 TTL 做短快取。常數放這裡是為了讓兩個模組共用同一個值，而不是各自定義後漂移。
"""

from __future__ import annotations

CACHE_TTL = 1.0  # ps / tmux 結果的短快取秒數，省掉同一次刷新內的重複呼叫
