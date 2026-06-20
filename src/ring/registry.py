"""RiNG 資料層：把「目前有哪些 Claude Code session 在台上」抓出來。

兩種來源，優先順序由高到低：

1. hook registry（精準模式）：``~/.config/ring/sessions/*.json``，由 RiNG 的 hook
   腳本在 SessionStart / Notification / UserPromptSubmit / Stop / SessionEnd
   等事件即時寫入。能精準知道「這個 session 正在等你」。
2. zero-config fallback：直接掃 ``~/.claude/projects/**/*.jsonl``，用檔案 mtime
   推活躍度，從記錄裡的 ``cwd`` 欄位還原真實路徑（避開目錄名以 ``-`` 編碼
   造成的 hyphen 還原歧義）。

額外富化：
- ``tmux_target``：靠 tmux pane 的 current_path 對 cwd，給你「去哪」的座標。
- ``todo``：解析 transcript 裡最新的 TodoWrite，給 done/total 真進度。

純 stdlib，不依賴任何第三方套件。
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ring.config import get_config

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
RING_REGISTRY = Path.home() / ".config" / "ring" / "sessions"

_CFG = get_config()
ACTIVE_WINDOW_SECONDS = _CFG.active_window_seconds  # 只看最近這段時間動過的 session（預設 6h）
WORKING_THRESHOLD_SECONDS = _CFG.working_threshold_seconds  # 多久沒動 → 🟢 工作中 變 🟡 閒置
_SUBPROCESS_CACHE_TTL = 1.0  # ps / tmux 結果的短快取，省掉同一次刷新內的重複呼叫


class Status(StrEnum):
    """場館裡一個 session 的狀態。等你的排最上面。"""

    WAITING = "waiting"  # 🔴 在等你進場（hook 模式才測得準）
    WORKING = "working"  # 🟢 台上正在跑
    IDLE = "idle"  # 🟡 一回合跑完、停著
    ENDED = "ended"  # ⚫ 已離場

    @property
    def rank(self) -> int:
        return {Status.WAITING: 0, Status.WORKING: 1, Status.IDLE: 2, Status.ENDED: 3}[self]

    @property
    def marker(self) -> str:
        return {Status.WAITING: "🔴", Status.WORKING: "🟢", Status.IDLE: "🟡", Status.ENDED: "⚫"}[self]


@dataclass
class Session:
    session_id: str
    cwd: str
    status: Status
    last_active: float
    last_action: str
    source: str  # "hook" | "scan"
    tmux_target: str | None = None  # e.g. "main:1.0"
    tty: str | None = None  # e.g. "/dev/ttys003"，給非-tmux 終端（iTerm2 等）聚焦用
    todo: tuple[int, int] | None = None  # (done, total)
    recent_actions: list[str] = field(default_factory=list)

    @property
    def project(self) -> str:
        return Path(self.cwd).name or self.cwd

    @property
    def idle_for(self) -> float:
        return max(0.0, time.time() - self.last_active)

    @property
    def location(self) -> str:
        """「去哪」：有 tmux 座標就給座標，否則給縮寫 cwd。"""
        if self.tmux_target:
            return self.tmux_target
        home = str(Path.home())
        return self.cwd.replace(home, "~", 1) if self.cwd.startswith(home) else self.cwd


def _tail_records(path: Path, max_tail: int = 128 * 1024) -> list[dict[str, Any]]:
    """讀 JSONL 檔尾若干筆合法 JSON 記錄（舊→新），只 seek 檔尾不整檔讀。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_tail:
                f.seek(size - max_tail)
                f.readline()  # 丟掉被切一半的那行
            chunk = f.read()
    except OSError:
        return []
    out = []
    for raw in chunk.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def _blocks(record: dict[str, Any]) -> list[Any]:
    msg = record.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
    return []


_CMD_NOISE = ("<local-command-stdout>", "<command-name>", "<command-message>", "<command-args>")


def _clean_text(s: str) -> str:
    s = s.strip()
    if any(marker in s for marker in _CMD_NOISE):
        return ""  # slash-command 的 stdout / 標記，不是真動作
    return s.splitlines()[0].strip() if s else ""  # 只取第一行，旁白長文不洗版


def _tool_summary(block: dict[str, Any]) -> str:
    """把一個 tool_use 變成簡短摘要，例如 → Edit foo.py / → Bash: git status。"""
    name = str(block.get("name") or "")
    if not name:
        return ""
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path")
        if path:
            return f"→ {name} {Path(str(path)).name}"
    elif name == "Bash":
        cmd = str(inp.get("command") or "").strip().replace("\n", " ")
        if cmd:
            return f"→ Bash: {cmd[:40]}"
    elif name in ("Grep", "Glob") and inp.get("pattern"):
        return f"→ {name} {inp['pattern']}"
    return f"→ {name}"


def _latest_action(records: list[dict[str, Any]]) -> str:
    """從新到舊找最近一個「真動作」：agent 的 tool_use 優先，其次 agent 的文字。

    跳過 user 訊息、tool_result、slash-command 輸出這類雜訊。
    """
    for record in reversed(records):
        for block in _blocks(record):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                summary = _tool_summary(block)
                if summary:
                    return summary
        if record.get("type") == "assistant":
            for block in _blocks(record):
                if isinstance(block, dict) and block.get("type") == "text":
                    txt = _clean_text(str(block.get("text", "")))
                    if txt:
                        return txt[:80]
    return "—"


def _extract_todo(records: list[dict[str, Any]]) -> tuple[int, int] | None:
    """從新到舊找最新的 TodoWrite，回傳 (done, total)。真進度訊號。"""
    for record in reversed(records):
        for block in _blocks(record):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "TodoWrite":
                todos = (block.get("input") or {}).get("todos")
                if isinstance(todos, list) and todos:
                    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
                    return done, len(todos)
    return None


def _recent_actions(records: list[dict[str, Any]], n: int = 5) -> list[str]:
    acts = []
    for record in records:
        for block in _blocks(record):
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                acts.append(str(block["name"]))
    return acts[-n:]


_tmux_cache: tuple[float, dict[str, str]] = (-1.0, {})


def _tmux_targets() -> dict[str, str]:
    """tmux pane current_path → "session:window.pane" 對照表。沒 tmux 就空。短快取。"""
    global _tmux_cache
    now = time.monotonic()
    if 0.0 <= now - _tmux_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _tmux_cache[1]
    mapping: dict[str, str] = {}
    try:
        out = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_current_path}\t#{session_name}:#{window_index}.#{pane_index}"],
            capture_output=True,
            text=True,
            timeout=3,
        )
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                if "\t" in line:
                    path, target = line.split("\t", 1)
                    mapping.setdefault(path, target)
    except (OSError, subprocess.SubprocessError):
        mapping = {}
    _tmux_cache = (now, mapping)
    return mapping


_pids_cache: tuple[float, list[int]] = (-1.0, [])


def running_claude_pids() -> list[int]:
    global _pids_cache
    now = time.monotonic()
    if 0.0 <= now - _pids_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _pids_cache[1]
    try:
        out = subprocess.run(["ps", "-Ao", "pid,comm"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    pids: list[int] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1].strip()) == "claude":
            try:
                pids.append(int(parts[0]))
            except ValueError:
                pass
    _pids_cache = (now, pids)
    return pids


def _pid_cwd(pid: int) -> str:
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    for line in out.splitlines():
        if line.startswith("n"):
            return line[1:]
    return ""


def _pid_tty(pid: int) -> str:
    """claude process 的控制終端，正規化成 iTerm2 認得的 "/dev/ttysNNN"。"""
    try:
        tty = subprocess.run(
            ["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True, timeout=3
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    if not tty or tty in ("??", "?"):
        return ""
    return tty if tty.startswith("/dev/") else f"/dev/{tty}"


def _claude_procs() -> list[tuple[str, str]]:
    """每個還活著的 claude：(cwd, tty)。cwd 判活躍/分流，tty 給終端跳轉。

    claude 不會把 session 的 jsonl 一直開著（lsof 看不到），但給得到 cwd 與 tty。
    同一個 cwd 可能同時開好幾個 session，所以之後在 cwd 群組裡只有 mtime 最新的
    這幾個算活著，其餘同專案的舊 session＝已離場。
    """
    procs: list[tuple[str, str]] = []
    for pid in running_claude_pids():
        cwd = _pid_cwd(pid)
        if cwd:
            procs.append((cwd, _pid_tty(pid)))
    return procs


def _scan_status(idle_seconds: float) -> Status:
    if idle_seconds < WORKING_THRESHOLD_SECONDS:
        return Status.WORKING
    if idle_seconds < ACTIVE_WINDOW_SECONDS:
        return Status.IDLE
    return Status.ENDED


def _scan_sessions(procs: list[tuple[str, str]]) -> list[Session]:
    if not CLAUDE_PROJECTS.is_dir():
        return []
    now = time.time()
    counts: dict[str, int] = {}
    cwd_ttys: dict[str, list[str]] = {}
    for cwd, tty in procs:
        counts[cwd] = counts.get(cwd, 0) + 1
        cwd_ttys.setdefault(cwd, []).append(tty)

    raw: list[Session] = []
    for project_dir in CLAUDE_PROJECTS.iterdir():
        if not project_dir.is_dir():
            continue
        for jsonl in project_dir.glob("*.jsonl"):
            try:
                mtime = jsonl.stat().st_mtime
            except OSError:
                continue
            if now - mtime > ACTIVE_WINDOW_SECONDS:
                continue
            records = _tail_records(jsonl)
            cwd, last_action = "", "—"
            if records:
                cwd = str(records[-1].get("cwd") or "")
                last_action = _latest_action(records)
            if not cwd:
                cwd = "/" + project_dir.name.lstrip("-").replace("-", "/")
            raw.append(
                Session(
                    session_id=jsonl.stem,
                    cwd=cwd,
                    status=Status.ENDED,  # 先佔位，下面按 cwd 群組判定
                    last_active=mtime,
                    last_action=last_action,
                    source="scan",
                    todo=_extract_todo(records),
                    recent_actions=_recent_actions(records),
                )
            )

    # 每個 cwd 群組裡，mtime 最新的 N 個＝活著（N=該 cwd 的 claude 數），其餘＝已離場。
    # 另外：working 門檻內還在寫的，無論如何算活著（cwd 比對失敗時的保險）。
    by_cwd: dict[str, list[Session]] = {}
    for s in raw:
        by_cwd.setdefault(s.cwd, []).append(s)
    out: list[Session] = []
    for cwd, group in by_cwd.items():
        group.sort(key=lambda s: s.last_active, reverse=True)
        live_n = counts.get(cwd, 0)
        # cwd 只有一個 claude 時，把它的 tty 給那個活著的 session（終端跳轉用）；
        # 多個 claude 同 cwd 無法精準對應，留給 hook 模式處理。
        uniq_tty = cwd_ttys[cwd][0] if live_n == 1 and cwd_ttys.get(cwd) else ""
        for i, s in enumerate(group):
            idle = now - s.last_active
            if i < live_n or idle < WORKING_THRESHOLD_SECONDS:
                s.status = _scan_status(idle)
            if i == 0 and uniq_tty:
                s.tty = uniq_tty
            out.append(s)
    return out


def _hook_sessions(procs: list[tuple[str, str]]) -> list[Session]:
    if not RING_REGISTRY.is_dir():
        return []
    out: list[Session] = []
    for f in RING_REGISTRY.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        try:
            todo = data.get("todo")
            out.append(
                Session(
                    session_id=str(data["session_id"]),
                    cwd=str(data.get("cwd", "")),
                    status=Status(data.get("status", "idle")),
                    last_active=float(data.get("last_active", 0.0)),
                    last_action=str(data.get("last_action", "—")),
                    source="hook",
                    tty=str(data.get("tty", "")) or None,
                    todo=tuple(todo) if isinstance(todo, list) and len(todo) == 2 else None,
                )
            )
        except (KeyError, ValueError):
            continue
    # SessionEnd 沒觸發（crash）會留下幽靈檔：cwd 底下沒有活著的 claude 就標已離場。
    if out:
        counts: dict[str, int] = {}
        for cwd, _tty in procs:
            counts[cwd] = counts.get(cwd, 0) + 1
        for s in out:
            if counts.get(s.cwd, 0) == 0:
                s.status = Status.ENDED
    return out
