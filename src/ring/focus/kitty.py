"""kitty 終端 focuser：透過 remote control socket 精準跳到分頁。

定位：kitty 官方沒有 tmux 那種「切換 client」指令，也沒有 iTerm2 / Terminal.app 的 AppleScript
可查詢；能精準定位到分頁的唯一途徑是它的 remote control 協定——連上 unix socket、列出視窗、
比對 tty，再叫它 focus-window。

機制與限制（誠實標明）：
- **kitty 預設關閉 remote control**，要在 kitty.conf 設 ``allow_remote_control`` 為
  ``yes`` / ``socket`` / ``socket-only``，並設 ``listen_on``（例：
  ``listen_on unix:/tmp/kitty-{kitty_pid}``）才會建立 socket；沒設就會安靜地跳過（回 ``None``），
  不會顯示錯誤。
- 只掃 ``/tmp`` 底下的 ``kitty-*`` socket（外加 ``$KITTY_LISTEN_ON`` 這條環境變數，但 RiNG
  通常跑在 kitty 之外，這個變數多半是空的）；``listen_on`` 指到別的目錄就配不到。
- 逐個 socket 問一輪：多個 kitty 實例的 socket 互不可見，沒有統一查詢點。
- 用 ``window["pid"]``／``window["foreground_processes"][]["pid"]`` 反查 tty 跟 session 比對，
  找到後呼叫 ``focus-window``；macOS 上固定補一次 ``osascript activate``（視窗縮到 Dock 時
  ``focus-window`` 只負責取消縮小、不負責把整個 app 帶到最前，已實測）。
- **Linux 端未實測**：``focus-window`` 底層走同一套 remote-control 協定，理論上等同
  ``wmctrl -i -a`` 的效果，但這只是推論。
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from ring.osascript import osascript
from ring.registry import Session

_SOCKET_DIRS: tuple[Path, ...] = (Path("/tmp"),)  # 之後要支援自訂 listen_on 目錄就加在這裡
_MAX_SOCKETS = 8


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    """跑一個唯讀／聚焦指令，成功回 ``CompletedProcess``（不論 returncode），失敗回 ``None``。"""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None


def _is_socket(path: Path) -> bool:
    try:
        return stat.S_ISSOCK(path.lstat().st_mode)
    except OSError:
        return False


def sockets() -> list[str]:
    """候選 kitty remote-control socket 路徑，依 mtime 新到舊排序，上限 ``_MAX_SOCKETS``。

    候選來源：``$KITTY_LISTEN_ON``（去掉 ``unix:`` 前綴、需為絕對路徑）＋ ``/tmp/kitty-*`` glob。
    一律用 ``stat.S_ISSOCK`` 過濾——``/tmp`` 下可能有 ``kitty-iterm-migration``、
    ``kitty-swift-module-cache`` 這種同前綴的普通目錄，不濾會誤連。呼叫時才做 I/O，
    模組 import 時不碰檔案系統。
    """
    candidates: list[Path] = []
    listen_on = os.environ.get("KITTY_LISTEN_ON", "")
    if listen_on.startswith("unix:"):
        listen_on = listen_on.removeprefix("unix:")
    if listen_on and Path(listen_on).is_absolute():
        candidates.append(Path(listen_on))
    for socket_dir in _SOCKET_DIRS:
        try:
            candidates.extend(sorted(socket_dir.glob("kitty-*")))
        except OSError:
            continue

    seen: set[Path] = set()
    found: list[tuple[float, Path]] = []
    for path in candidates:
        if path in seen or not _is_socket(path):
            continue
        seen.add(path)
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue
        found.append((mtime, path))
    found.sort(key=lambda item: item[0], reverse=True)
    return [str(path) for _mtime, path in found[:_MAX_SOCKETS]]


def _pid_tty_map() -> dict[int, str]:
    """一次性 ``ps -eo pid=,tty=`` 快照：pid → ``/dev/ttyXXX``（``?``/``??`` 視為無 tty）。"""
    result = _run(["ps", "-eo", "pid=,tty="])
    table: dict[int, str] = {}
    if result is None or result.returncode != 0:
        return table
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        raw_tty = parts[1]
        if not raw_tty or raw_tty in {"?", "??"}:
            continue
        table[pid] = raw_tty if raw_tty.startswith("/dev/") else f"/dev/{raw_tty}"
    return table


def _window_matches_tty(window: dict[str, Any], tty: str, pid_tty: dict[int, str]) -> bool:
    pids: list[Any] = [window.get("pid")]
    foreground = window.get("foreground_processes")
    if isinstance(foreground, list):
        for proc in foreground:
            if isinstance(proc, dict):
                pids.append(proc.get("pid"))
    return any(isinstance(pid, int) and pid_tty.get(pid) == tty for pid in pids)


def _match_window(os_windows: Any, tty: str, pid_tty: dict[int, str]) -> int | None:
    """走訪 ``kitty @ ls`` 的 JSON（OS window → tabs[] → windows[]），回第一個比對到 tty 的 window id。"""
    if not isinstance(os_windows, list):
        return None
    for os_window in os_windows:
        if not isinstance(os_window, dict):
            continue
        tabs = os_window.get("tabs")
        if not isinstance(tabs, list):
            continue
        for tab in tabs:
            if not isinstance(tab, dict):
                continue
            windows = tab.get("windows")
            if not isinstance(windows, list):
                continue
            for window in windows:
                if not isinstance(window, dict):
                    continue
                if _window_matches_tty(window, tty, pid_tty):
                    win_id = window.get("id")
                    if isinstance(win_id, int):
                        return win_id
    return None


def resolve_window(tty: str) -> tuple[str, int] | None:
    """tty → ``(socket 路徑, kitty window id)``；沒裝 kitty／沒 socket／配不到 → ``None``。

    這段定位邏輯同時給 ``focus``（聚焦分頁）與 ``permission``（抓畫面／送鍵）兩邊共用，
    kitty ``ls`` 的 JSON 形狀知識只此一份，兩邊不重寫第二份。
    """
    if not tty or shutil.which("kitty") is None:
        return None
    socks = sockets()
    if not socks:
        return None

    pid_tty = _pid_tty_map()

    for sock in socks:
        result = _run(["kitty", "@", "--to", f"unix:{sock}", "ls"])
        if result is None or result.returncode != 0:
            continue
        try:
            os_windows = json.loads(result.stdout)
        except json.JSONDecodeError:
            continue
        win_id = _match_window(os_windows, tty, pid_tty)
        if win_id is None:
            continue
        return sock, win_id
    return None


class KittyFocuser:
    name = "kitty"

    def try_focus(self, session: Session) -> tuple[bool, str] | None:
        tty = session.tty
        if not tty:
            return None
        resolved = resolve_window(tty)
        if resolved is None:
            return None
        sock, win_id = resolved

        focus_result = _run(["kitty", "@", "--to", f"unix:{sock}", "focus-window", "--match", f"id:{win_id}"])
        if focus_result is None or focus_result.returncode != 0:
            return False, f"kitty focus-window failed for id:{win_id}"
        if sys.platform == "darwin" and shutil.which("osascript"):
            osascript('tell application "kitty" to activate')
        return True, f"{self.name} {win_id}"


focuser = KittyFocuser()
