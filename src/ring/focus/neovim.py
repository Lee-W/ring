"""Neovim terminal focuser：切回承載 session 的 ``:terminal`` buffer。

Neovim 的 terminal job 會繼承 ``NVIM``（server socket）環境變數。從 session 的 tty
找到該 job，再沿 parent process chain 找到 owning nvim，就能透過 remote API 精準切換
buffer。這個 focuser 是「前置聚焦」：切完內層 buffer 後，dispatcher 仍會繼續讓 tmux /
iTerm2 / Terminal.app 把外層 pane 或視窗帶到前景。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from shutil import which

from ring.registry import Session

_NVIM_NAMES = {"nvim", "nvim-bin"}
_NVIM_ENV_RE = re.compile(r"(?:^|\s)NVIM=([^\s]+)")


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None


def _tty_pids(tty: str) -> list[int]:
    result = _run(["ps", "-o", "pid=", "-t", tty.removeprefix("/dev/")])
    if result is None or result.returncode != 0:
        return []
    pids: list[int] = []
    for value in result.stdout.split():
        try:
            pids.append(int(value))
        except ValueError:
            continue
    return pids


def _process_table() -> tuple[dict[int, int], dict[int, str]]:
    result = _run(["ps", "-eo", "pid=,ppid=,comm="])
    if result is None or result.returncode != 0:
        return {}, {}
    parents: dict[int, int] = {}
    commands: dict[int, str] = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) != 3:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        parents[pid] = ppid
        commands[pid] = os.path.basename(parts[2])
    return parents, commands


def _ancestors(pid: int, parents: dict[int, int]) -> list[int]:
    chain: list[int] = []
    seen: set[int] = set()
    current = pid
    while current > 1 and current not in seen:
        seen.add(current)
        chain.append(current)
        current = parents.get(current, 0)
    return chain


def _nvim_address(pid: int) -> str:
    """讀 process 繼承到的 ``NVIM``；Linux 優先用不會混入 argv 的 procfs。"""
    if sys.platform.startswith("linux"):
        try:
            entries = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            entries = []
        for entry in entries:
            if entry.startswith(b"NVIM="):
                return entry.removeprefix(b"NVIM=").decode(errors="surrogateescape")

    result = _run(["ps", "eww", "-p", str(pid), "-o", "command="])
    if result is None or result.returncode != 0:
        return ""
    match = _NVIM_ENV_RE.search(result.stdout)
    return match.group(1) if match else ""


def _pid_tty(pid: int) -> str:
    result = _run(["ps", "-o", "tty=", "-p", str(pid)])
    if result is None or result.returncode != 0:
        return ""
    tty = result.stdout.strip()
    if not tty or tty in {"?", "??"}:
        return ""
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def _find_neovim(tty: str) -> tuple[str, int] | None:
    parents, commands = _process_table()
    for pid in _tty_pids(tty):
        address = _nvim_address(pid)
        if not address:
            continue
        for ancestor in _ancestors(pid, parents):
            if commands.get(ancestor) in _NVIM_NAMES:
                return address, ancestor
    return None


def _remote_expr(tty: str) -> str:
    """建立在 server 端找 terminal job tty、切 tab/window/buffer 的 Lua expression。"""
    target = json.dumps(tty)
    lua = f"""(function()
  local target = {target}
  local function job_tty(pid)
    local value = vim.trim(vim.fn.system({{'ps', '-o', 'tty=', '-p', tostring(pid)}}))
    if value == '' or value == '?' or value == '??' then return '' end
    if vim.startswith(value, '/dev/') then return value end
    return '/dev/' .. value
  end
  for _, buf in ipairs(vim.api.nvim_list_bufs()) do
    local ok, job = pcall(vim.api.nvim_buf_get_var, buf, 'terminal_job_id')
    if ok and job_tty(vim.fn.jobpid(job)) == target then
      for _, tab in ipairs(vim.api.nvim_list_tabpages()) do
        for _, win in ipairs(vim.api.nvim_tabpage_list_wins(tab)) do
          if vim.api.nvim_win_get_buf(win) == buf then
            vim.api.nvim_set_current_tabpage(tab)
            vim.api.nvim_set_current_win(win)
            vim.cmd('startinsert')
            return buf
          end
        end
      end
      vim.api.nvim_set_current_buf(buf)
      vim.cmd('startinsert')
      return buf
    end
  end
  return 0
end)()"""
    return f"luaeval({json.dumps(lua)})"


class NeovimFocuser:
    name = "Neovim"
    continue_after_success = True

    def try_focus(self, session: Session) -> tuple[bool, str] | None:
        tty = session.tty
        nvim = which("nvim")
        if not tty or not nvim:
            return None
        found = _find_neovim(tty)
        if found is None:
            return None
        address, nvim_pid = found
        result = _run([nvim, "--server", address, "--remote-expr", _remote_expr(tty)])
        if result is None:
            return False, "nvim remote request failed"
        if result.returncode != 0:
            return False, result.stderr.strip() or "nvim remote request failed"
        try:
            buffer = int(result.stdout.strip())
        except ValueError:
            buffer = 0
        if buffer <= 0:
            return False, f"terminal buffer for {tty} not found"

        # 後續外層 focuser 要比對 nvim 自己所在的 tty，而不是內層 :terminal job 的 tty。
        outer_tty = _pid_tty(nvim_pid)
        if outer_tty:
            session.tty = outer_tty
        return True, f"Neovim buffer {buffer}"


focuser = NeovimFocuser()
