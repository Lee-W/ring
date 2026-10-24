"""檔案系統小工具——跨 process 的鎖與原子寫入，集中在這裡避免各模組各抄一份而分岔。

- ``file_lock(path)``：以 ``<path>.lock`` 旁檔做 ``fcntl.flock`` 獨占。鎖檔獨立於資料檔，
  資料檔被 ``os.replace`` 換掉 inode 後，等候中的 writer 仍會鎖到同一把鎖；鎖檔不隨資料刪除。
- ``atomic_write_text(path, text)``：同目錄唯一名 tmp ＋ fsync ＋ ``os.replace``，
  讀者要嘛看到舊內容、要嘛看到完整新內容。
- ``quarantine_file(path, stamp=...)``：壞檔改名保留，而不是被下一次寫入無聲蓋掉。
- ``backup_file(path)``：寫入前保留一份 ``<name>.bak``（覆寫舊 bak，不累積）。
"""

from __future__ import annotations

import contextlib
import fcntl
import os
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path

_NEW_FILE_MODE = 0o644


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """跨 process 的 read-modify-write 臨界區；鎖的是 ``<path>.lock`` 旁檔。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_name(path.name + ".lock").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def atomic_write_text(path: Path, text: str) -> None:
    """原子寫入：tmp 在同目錄且名稱唯一，fsync 後 ``os.replace``；失敗會清掉 tmp 並往上拋。

    沿用既有檔的權限（沒有就用 0644），不讓 ``mkstemp`` 的 0600 悄悄改掉使用者檔案的權限。
    """
    path = Path(os.path.realpath(path))  # 跟進 symlink（dotfiles 管理的設定檔），不把連結換成一般檔
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o7777
    except OSError:
        mode = _NEW_FILE_MODE
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp.chmod(mode)
        tmp.replace(path)
    except BaseException:
        with contextlib.suppress(OSError):
            tmp.unlink()
        raise


def backup_file(path: Path) -> Path | None:
    """原檔存在才備份成 ``<name>.bak``；回傳 bak 路徑，原檔不存在回 None。

    每次都覆寫舊 bak：它是「上一次寫入前的前像」，不是歷史。對 symlink 備份真實檔；
    複製失敗會拋 ``OSError``，由呼叫端決定如何中止（不要在備份失敗時照樣覆寫原檔）。
    """
    real = Path(os.path.realpath(path))
    if not real.exists():
        return None
    bak = real.with_name(real.name + ".bak")
    shutil.copy2(real, bak)
    return bak


def quarantine_file(path: Path, *, stamp: str) -> Path | None:
    """把壞檔改名成 ``<name>.corrupt-<stamp>`` 保留下來；成功回新路徑，失敗回 None。"""
    target = path.with_name(f"{path.name}.corrupt-{stamp}")
    try:
        path.replace(target)
    except OSError:
        return None
    return target
