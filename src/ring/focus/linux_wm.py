"""Linux X11 視窗 focuser（best-effort）：用 session 的 tty 找到擁有它的終端視窗、帶到前景。

定位是 macOS 之外的 fallback——Linux 上沒跑 tmux 的 session 原本完全跳不回去
（tmux 已由 tmux focuser 處理、iTerm2 / Terminal.app 是 macOS 限定），這個 focuser 補上那個洞。

機制與限制（誠實標明）：
- 只在 Linux 且裝了 ``wmctrl`` 時接手；否則回 ``None`` 交給別的 focuser。
- 從 tty 上的 process 往上追到「擁有 X11 視窗」的祖先（多數終端模擬器——xterm / konsole /
  alacritty / kitty / xfce4-terminal——是 shell 的祖先），用 ``wmctrl`` 把那個視窗帶到前景。
- **X11 限定**：Wayland 下 ``wmctrl`` 通常無效。
- **無法選分頁**：只能把整個終端視窗帶到前景，無法切到正確分頁（X11 沒有跨終端的分頁 API）。
- gnome-terminal 走 client/server，視窗 pid 不在 shell 的祖先鏈上，可能配不到（回 ``None``）。
"""

from __future__ import annotations

import shutil
import subprocess
import sys

from ring.registry import Session


def _run(cmd: list[str]) -> str | None:
    """跑一個唯讀／聚焦指令，成功回 stdout（可能為空字串），失敗回 ``None``。"""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def _tty_pids(tty: str) -> list[int]:
    """某 tty 上的所有 process pid。``tty`` 形如 ``/dev/pts/3``。"""
    out = _run(["ps", "-o", "pid=", "-t", tty.removeprefix("/dev/")])
    if out is None:
        return []
    pids = []
    for tok in out.split():
        try:
            pids.append(int(tok))
        except ValueError:
            continue
    return pids


def _ppid_map() -> dict[int, int]:
    """全系統 pid → ppid 對照表。"""
    out = _run(["ps", "-eo", "pid=,ppid="])
    if out is None:
        return {}
    table: dict[int, int] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            table[int(parts[0])] = int(parts[1])
        except ValueError:
            continue
    return table


def _ancestors(pid: int, ppid_map: dict[int, int]) -> list[int]:
    """``pid`` 自身 + 一路往上的祖先（由近到遠），遇環或到 init(1) 即止。"""
    chain: list[int] = []
    seen: set[int] = set()
    cur = pid
    while cur and cur > 1 and cur not in seen:
        seen.add(cur)
        chain.append(cur)
        cur = ppid_map.get(cur, 0)
    return chain


def _window_pids() -> dict[int, str]:
    """``wmctrl -lp`` 解析：擁有 X11 視窗的 pid → window id（同 pid 多視窗取第一個）。"""
    out = _run(["wmctrl", "-lp"])
    if out is None:
        return {}
    mapping: dict[int, str] = {}
    for line in out.splitlines():
        parts = line.split(None, 4)  # winid desktop pid host title
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[2])
        except ValueError:
            continue
        mapping.setdefault(pid, parts[0])
    return mapping


class LinuxWMFocuser:
    name = "linux-wm"

    def try_focus(self, session: Session) -> tuple[bool, str] | None:
        tty = session.tty
        if not sys.platform.startswith("linux") or not tty or not shutil.which("wmctrl"):
            return None
        tty_pids = _tty_pids(tty)
        if not tty_pids:
            return None
        ppid_map = _ppid_map()
        win_pids = _window_pids()
        for start in tty_pids:
            for pid in _ancestors(start, ppid_map):
                winid = win_pids.get(pid)
                if not winid:
                    continue
                if _run(["wmctrl", "-i", "-a", winid]) is not None:
                    return True, f"{self.name} {winid}"
                return False, f"wmctrl activate failed for {winid}"
        return None  # 配不到視窗 → 交給別的 focuser，最後由 jump() 回報 tty 可能已關閉


focuser = LinuxWMFocuser()
