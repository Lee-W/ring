"""cwd 比對用的路徑正規化helpers。

「session 記的 cwd」與「live process 量到的 cwd」來源不同（前者是字面路徑，後者已解
過 symlink），直接字串比對會誤判。這裡的函式只做比對用的正規化，不改動任何顯示值。
"""

from __future__ import annotations

import os


def _real(path: str) -> str:
    """正規化路徑供「session cwd ↔ live process cwd」比對。

    lsof 回報的是解析過 symlink 的真實路徑，但 hook / JSONL / sqlite 記的常是字面
    路徑；兩者直接字串比對，遇到 symlink 專案路徑會對不上，導致活著的 session 被誤判
    離場（counts 為 0）或被補成重複列。各自 realpath 後再比對即可避免。只用於比對鍵，
    不改動 ``Session.cwd`` 的顯示值。
    """
    if not path:
        return path
    try:
        return os.path.realpath(path)
    except OSError:
        return path


def _is_ancestor_dir(ancestor: str, path: str) -> bool:
    """``ancestor`` 是否為 ``path`` 本身或其祖先目錄（兩者皆須已用 ``_real`` 正規化）。

    純字串 ``startswith`` 裸比對會誤判 ``/foo`` 命中 ``/foobar``；用尾斜線組出前綴
    （或直接相等）才能正確界定「目錄」邊界。
    """
    if not ancestor or not path:
        return False
    if ancestor == path:
        return True
    prefix = ancestor if ancestor.endswith(os.sep) else ancestor + os.sep
    return path.startswith(prefix)


def _has_ancestor_live_process(row_cwd: str, live_cwds: list[str]) -> bool:
    """``live_cwds`` 裡是否有任一筆是 ``row_cwd`` 本身或其祖先目錄。

    用於：使用者在 session 裡 cd 進子目錄後，hook payload 記的 cwd 變成子目錄，但
    claude process 實際 cwd（lsof 量到的）仍停在啟動目錄——子目錄底下量不到 live
    process，不代表 session 已離場，只是 process 沒跟著 cd。
    """
    return any(_is_ancestor_dir(live_cwd, row_cwd) for live_cwd in live_cwds)
