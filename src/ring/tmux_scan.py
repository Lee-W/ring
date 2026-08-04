"""tmux pane / process tree 的掃描層：把 session 對應到可聚焦的 tmux target。

跑 ``tmux list-panes`` 與 ``ps`` 並在同一次刷新內短快取（TTL 見 ``ring.subproc``）。
本模組不認識 ``Session`` 的建構，只讀它的 ``cwd`` / ``session_id`` 做配對，所以型別
以 TYPE_CHECKING 匯入，執行期不與 ``ring.registry`` 相依。
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ring.pathutil import _real
from ring.subproc import CACHE_TTL

if TYPE_CHECKING:
    from ring.registry import Session

_tmux_cache: tuple[float, dict[str, str]] = (-1.0, {})


_tmux_panes_cache: tuple[float, list[TmuxPane]] = (-1.0, [])


_process_rows_cache: tuple[float, dict[int, tuple[int, str]]] = (-1.0, {})


@dataclass(frozen=True)
class TmuxPane:
    pane_id: str
    cwd: str
    target: str
    tty: str = ""
    pane_pid: int | None = None


def _tmux_panes() -> list[TmuxPane]:
    """目前 tmux panes 的可聚焦座標。短快取。"""
    global _tmux_panes_cache
    now = time.monotonic()
    if 0.0 <= now - _tmux_panes_cache[0] <= CACHE_TTL:
        return _tmux_panes_cache[1]
    panes: list[TmuxPane] = []
    try:
        out = subprocess.run(
            [
                "tmux",
                "list-panes",
                "-a",
                "-F",
                "#{pane_id}\t#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}\t#{pane_tty}\t#{pane_pid}",
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                parts = line.split("\t")
                if len(parts) != 5:
                    continue
                pane_id, cwd, target, tty, pane_pid = parts
                try:
                    parsed_pid = int(pane_pid)
                except ValueError:
                    parsed_pid = None
                panes.append(TmuxPane(pane_id=pane_id, cwd=cwd, target=target, tty=tty, pane_pid=parsed_pid))
    except (OSError, subprocess.SubprocessError):
        panes = []
    _tmux_panes_cache = (now, panes)
    return panes


def _tmux_targets() -> dict[str, str]:
    """tmux pane current_path → "session:window.pane" 對照表。沒 tmux 就空。短快取。"""
    global _tmux_cache
    now = time.monotonic()
    if 0.0 <= now - _tmux_cache[0] <= CACHE_TTL:
        return _tmux_cache[1]
    mapping: dict[str, str] = {}
    for pane in _tmux_panes():
        mapping.setdefault(pane.cwd, pane.target)
    _tmux_cache = (now, mapping)
    return mapping


def _tmux_targets_by_cwd() -> dict[str, list[str]]:
    """tmux pane current_path → 所有候選 target。供同 cwd fallback 依序分配。"""
    mapping: dict[str, list[str]] = {}
    for pane in _tmux_panes():
        mapping.setdefault(pane.cwd, []).append(pane.target)
    return mapping


def _tmux_pane_targets() -> dict[str, str]:
    """tmux pane id → target。pane 不存在時不會出現在結果裡，呼叫端自然 fallback。"""
    return {pane.pane_id: pane.target for pane in _tmux_panes()}


def _process_rows() -> dict[int, tuple[int, str]]:
    """pid → (ppid, args)。給 scan-only tmux pane process-tree 消歧用；同輪共用短快取。"""
    global _process_rows_cache
    now = time.monotonic()
    if 0.0 <= now - _process_rows_cache[0] <= CACHE_TTL:
        return _process_rows_cache[1]
    try:
        out = subprocess.run(["ps", "-Ao", "pid=,ppid=,args="], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return {}
    rows: dict[int, tuple[int, str]] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        rows[pid] = (ppid, parts[2] if len(parts) == 3 else "")
    _process_rows_cache = (now, rows)
    return rows


def _descendant_pids(root_pid: int, rows: dict[int, tuple[int, str]]) -> set[int]:
    children: dict[int, list[int]] = {}
    for pid, (ppid, _args) in rows.items():
        children.setdefault(ppid, []).append(pid)
    found: set[int] = set()
    stack = list(children.get(root_pid, []))
    while stack:
        pid = stack.pop()
        if pid in found:
            continue
        found.add(pid)
        stack.extend(children.get(pid, []))
    return found


def _tmux_process_tree_targets(sessions: list[Session]) -> dict[str, str]:
    """scan-only 消歧：pane 子孫 process args 明確提到 session id 時，配到該 pane。

    這是刻意保守的規則：只接受 process tree 內有 session id 這種強訊號；沒有就回空，
    讓呼叫端走 cwd fallback，避免把同 cwd session 硬猜錯。
    """
    candidates = [s for s in sessions if not s.tmux_pane and s.session_id]
    if not candidates:
        return {}
    rows = _process_rows()
    if not rows:
        return {}

    result: dict[str, str] = {}
    for pane in _tmux_panes():
        if pane.pane_pid is None:
            continue
        pids = _descendant_pids(pane.pane_pid, rows)
        if not pids:
            continue
        # local AI 沒有 transcript session id 可放進 argv，但 session id 自帶真實 pid。
        # 直接用 pane process tree 對 pid，比同 cwd 依序猜 pane 精準。
        for s in candidates:
            if s.session_id in result:
                continue
            prefix = f"{s.provider}:pid-"
            if not s.session_id.startswith(prefix):
                continue
            try:
                process_pid = int(s.session_id[len(prefix) :])
            except ValueError:
                continue
            if process_pid in pids:
                result[s.session_id] = pane.target
        args_text = "\n".join(rows[pid][1] for pid in pids if pid in rows)
        if not args_text:
            continue
        for s in candidates:
            if s.session_id in result:
                continue
            if _real(s.cwd) != _real(pane.cwd):
                continue
            if s.session_id in args_text:
                result[s.session_id] = pane.target
    return result
