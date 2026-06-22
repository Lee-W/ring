"""RiNG 資料層：把「目前有哪些 Claude Code session 在台上」抓出來。

兩種來源，優先順序由高到低：

1. hook registry（精準模式）：``~/.config/ring/sessions/*.json``，由 RiNG 的 hook
   腳本在 SessionStart / Notification / UserPromptSubmit / Stop / SessionEnd
   等事件即時寫入。能精準知道「這個 session 需要你決策」。
2. zero-config fallback：直接掃 ``~/.claude/projects/**/*.jsonl``，用檔案 mtime
   推活躍度，從記錄裡的 ``cwd`` 欄位還原真實路徑（避開目錄名以 ``-`` 編碼
   造成的 hyphen 還原歧義）。scan 模式不把「回完一輪」當成 🔴 WAITING；
   WAITING 只保留給 hook 可確認的權限 / 選項等互動。

額外富化：
- ``tmux_target``：靠 tmux pane 的 current_path 對 cwd，給你「去哪」的座標。
- ``todo``：解析 transcript 裡最新的 TodoWrite，給 done/total 真進度。

純 stdlib，不依賴任何第三方套件。
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from ring.config import get_config

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
RING_REGISTRY = Path.home() / ".config" / "ring" / "sessions"
CODEX_STATE = Path.home() / ".codex" / "state_5.sqlite"

_CFG = get_config()
ACTIVE_WINDOW_SECONDS = _CFG.active_window_seconds  # 只看最近這段時間動過的 session（預設 6h）
WORKING_THRESHOLD_SECONDS = _CFG.working_threshold_seconds  # 多久沒動 → 🟢 工作中 變 🟡 閒置
WAITING_WINDOW_SECONDS = _CFG.waiting_window_seconds  # IDLE 升 WAITING 的時間窗上限（預設 30 分）
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
    source: str  # "hook" | "scan" | "proc"
    tmux_target: str | None = None  # e.g. "main:1.0"
    tty: str | None = None  # e.g. "/dev/ttys003"，給非-tmux 終端（iTerm2 等）聚焦用
    todo: tuple[int, int] | None = None  # (done, total)
    recent_actions: list[str] = field(default_factory=list)
    provider: str = ""
    _tail_kind: str = field(default="none", repr=False, compare=False)  # 內部：scan 路徑暫存對話尾判定
    origin_cwd: str = ""  # 開場 cwd（session 第一筆帶 cwd 紀錄），用於歸屬；空時 fallback 到 cwd

    @property
    def project(self) -> str:
        """session 所屬專案名稱。

        優先用 ``origin_cwd``（開場 cwd）——確保中途 ``cd`` 過的 session 仍歸屬到
        它真正的專案，而非漂到目的地專案。``origin_cwd`` 未設時 fallback 到 ``cwd``，
        行為與舊版一致（hook / proc 等來源的 cwd 本就穩定）。
        """
        base = self.origin_cwd or self.cwd
        return Path(base).name or base

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


def _head_cwd(path: Path, max_head: int = 128 * 1024) -> str:
    """讀 JSONL 檔頭，回傳第一筆含非空 ``cwd`` 欄位的紀錄之 cwd。

    目的：修補 scan 模式「中途 cd 漂移」問題。Claude Code session 中途 ``cd``
    後，新紀錄的 ``cwd`` 已指向目的地目錄，若只看檔尾（``_tail_records``）會
    誤判 session 歸屬。改讀檔頭取「開場 cwd」才能把 session 留在它真正所屬的
    專案列，而非漂到目的地專案。

    跳過 JSONL 裡無 ``cwd`` 的 meta 筆（last-prompt、mode、permission-mode、
    file-history-snapshot 等）；命中即停，不必整檔讀完。

    :param path:     JSONL 檔案路徑。
    :param max_head: 從檔頭最多讀取的位元組數，預設 128 KiB，足以涵蓋早期幾十筆紀錄。
    :returns:        第一筆非空 cwd 字串；找不到（空檔、全無 cwd）回 ``""``。
    """
    try:
        with path.open("rb") as f:
            chunk = f.read(max_head)
    except OSError:
        return ""
    for raw in chunk.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cwd = record.get("cwd")
        if cwd and isinstance(cwd, str):
            return cwd
    return ""


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


def _conversation_tail_kind(records: list[dict[str, Any]]) -> str:
    """從對話尾巴往回走，判定最近一個「真訊息紀錄」的性質。

    回傳值（str）：
    - ``"waiting"``    : 對話最後是 assistant end_turn 收尾，Claude 回完一輪。
    - ``"working"``    : 對話最後是真人送出的 prompt，輪到 Claude 回應。
    - ``"interrupted"`` : 工具呼叫進行中（assistant tool_use / user tool_result），尚未完成一輪。
    - ``"none"``       : 沒有可判定的訊息（空 records、全噪音、無 assistant/user 訊息）。

    演算法：從 ``reversed(records)`` 往回走，跳過噪音，找第一個真訊息紀錄，
    依 type 與 content 判定後立即 return。絕不直接看 records[-1]——尾巴幾乎都是噪音。

    限制（誠實標明）：靠對話尾巴猜。Notification（卡權限等授權流程）在 JSONL 不留痕跡，
    scan 模式測不到——那種需要使用者決策的狀態仍需 hook 模式偵測。
    """
    for record in reversed(records):
        t = record.get("type")
        # --- 跳過噪音 ---
        if t not in ("user", "assistant"):
            continue  # file-history-snapshot / system / permission-mode / mode / last-prompt 等
        if record.get("isMeta") is True:
            continue
        if record.get("isSidechain") is True:
            continue
        # --- 第一個真訊息紀錄，依 type 判定 ---
        if t == "assistant":
            msg = record.get("message")
            stop_reason = msg.get("stop_reason") if isinstance(msg, dict) else None
            # assistant 帶 tool_use block → 工具呼叫進行中
            if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in _blocks(record)):
                return "interrupted"
            if stop_reason == "end_turn":
                return "waiting"
            # tool_use / stop_sequence / None / 其他 → 視為中途被打斷
            return "interrupted"
        # t == "user"
        # user 帶工具結果（toolUseResult 欄位，或 content 含 tool_result block）→ 中途
        if record.get("toolUseResult") is not None:
            return "interrupted"
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(record)):
            return "interrupted"
        # command 噪音（slash-command stdout）→ 跳過，繼續往回走
        text_blocks = [b for b in _blocks(record) if isinstance(b, dict) and b.get("type") == "text"]
        if text_blocks and all(_clean_text(str(b.get("text", ""))) == "" for b in text_blocks):
            continue
        # 真人 prompt → 輪到 Claude 回應，不是回合結束
        return "working"
    return "none"


def _apply_waiting(
    status: Status,
    idle_seconds: float,
    tail_kind: str,
    waiting_window: float,
) -> Status:
    """對話尾是 end_turn 且在時間窗內時，將 live/idle scan row 收斂為 IDLE。

    純函式、可單測，不依賴 module-level 常數。
    不把回合結束升成 WAITING；WAITING 只保留給 hook 確認的權限 / 選項互動：
    - WORKING（< 90s）：若尾端已是 end_turn，代表回合其實結束了，收斂成 IDLE。
    - ENDED：超過活躍窗，不升。
    """
    if status in {Status.WORKING, Status.IDLE} and tail_kind == "waiting" and idle_seconds < waiting_window:
        return Status.IDLE
    return status


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
_codex_pids_cache: tuple[float, list[int]] = (-1.0, [])


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


def running_codex_pids() -> list[int]:
    global _codex_pids_cache
    now = time.monotonic()
    if 0.0 <= now - _codex_pids_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _codex_pids_cache[1]
    try:
        out = subprocess.run(["ps", "-Ao", "pid,comm"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    pids: list[int] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 1)
        if len(parts) == 2 and os.path.basename(parts[1].strip()) == "codex":
            try:
                pids.append(int(parts[0]))
            except ValueError:
                pass
    _codex_pids_cache = (now, pids)
    return pids


def running_agent_pids() -> list[int]:
    """所有內建來源看得到的 live agent CLI 行程。"""
    return [*running_claude_pids(), *running_codex_pids()]


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


def _codex_procs() -> list[tuple[str, str]]:
    """每個還活著的 Codex CLI：(cwd, tty)。"""
    procs: list[tuple[str, str]] = []
    for pid in running_codex_pids():
        cwd = _pid_cwd(pid)
        if cwd:
            procs.append((cwd, _pid_tty(pid)))
    return procs


def _codex_tail_kind(records: list[dict[str, Any]]) -> str:
    """判定 Codex rollout 尾端狀態。

    回傳值：
    - ``"waiting"``：Codex 已完成一輪、回到等使用者輸入。
    - ``"working"``：最後仍在處理使用者輸入或工具呼叫。
    - ``"none"``：沒有可判斷事件。
    """
    for record in reversed(records):
        record_type = record.get("type")
        payload = record.get("payload")
        if not isinstance(payload, dict):
            payload = {}
        if record_type == "event_msg":
            event_type = payload.get("type")
            if event_type == "task_complete":
                return "waiting"
            if event_type in {"task_started", "user_message", "agent_message"}:
                return "working"
        if record_type == "response_item":
            item_type = payload.get("type")
            if item_type == "message":
                if payload.get("role") == "assistant" and payload.get("phase") == "final_answer":
                    return "waiting"
                return "working"
            if item_type in {"function_call", "function_call_output"}:
                return "working"
    return "none"


def _codex_latest_action(records: list[dict[str, Any]], fallback: str) -> str:
    """從 Codex rollout 尾端取簡短動作摘要。"""
    for record in reversed(records):
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        if record.get("type") == "response_item" and payload.get("type") == "function_call":
            name = str(payload.get("name") or "").strip()
            if name:
                return f"→ {name}"
        if record.get("type") == "event_msg" and payload.get("type") == "agent_message":
            msg = str(payload.get("message") or "").strip()
            if msg:
                return msg.splitlines()[0][:80]
    return fallback or "—"


def _codex_threads(procs: list[tuple[str, str]]) -> list[Session]:
    """從 Codex state sqlite 讀 thread，並用 live codex process 粗略判斷活性。"""
    if not CODEX_STATE.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{CODEX_STATE}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            """
            select id, cwd, title, rollout_path, preview, updated_at, updated_at_ms
            from threads
            where archived = 0
            order by coalesce(nullif(updated_at_ms, 0), updated_at * 1000) desc
            limit 200
            """
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        try:
            con.close()
        except UnboundLocalError:
            pass

    now = time.time()
    counts: dict[str, int] = {}
    cwd_ttys: dict[str, list[str]] = {}
    for cwd, tty in procs:
        counts[cwd] = counts.get(cwd, 0) + 1
        cwd_ttys.setdefault(cwd, []).append(tty)

    raw: list[Session] = []
    for row in rows:
        cwd = str(row["cwd"] or "")
        if not cwd:
            continue
        updated_ms = int(row["updated_at_ms"] or 0)
        last_active = updated_ms / 1000 if updated_ms else float(row["updated_at"] or 0)
        if now - last_active > ACTIVE_WINDOW_SECONDS and counts.get(cwd, 0) == 0:
            continue
        rollout_path = Path(str(row["rollout_path"] or ""))
        records = _tail_records(rollout_path) if rollout_path else []
        title = str(row["title"] or row["preview"] or "")
        tail_kind = _codex_tail_kind(records)
        raw.append(
            Session(
                session_id=f"codex:{row['id']}",
                cwd=cwd,
                status=Status.ENDED,
                last_active=last_active,
                last_action=_codex_latest_action(records, title),
                source="codex",
                provider="codex",
                _tail_kind=tail_kind,
                origin_cwd=cwd,
            )
        )

    by_cwd: dict[str, list[Session]] = {}
    for s in raw:
        by_cwd.setdefault(s.cwd, []).append(s)

    out: list[Session] = []
    for cwd, group in by_cwd.items():
        group.sort(key=lambda s: s.last_active, reverse=True)
        live_n = counts.get(cwd, 0)
        uniq_tty = cwd_ttys[cwd][0] if live_n == 1 and cwd_ttys.get(cwd) else ""
        for i, s in enumerate(group):
            if i < live_n:
                idle = now - s.last_active
                s.status = Status.IDLE if s._tail_kind == "waiting" else _scan_status(idle)
                if i == 0 and uniq_tty:
                    s.tty = uniq_tty
            out.append(s)
    return out


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
            origin = _head_cwd(jsonl)
            if not cwd and not origin:
                # 兩者皆空時走 dash-還原 fallback，origin 與 cwd 退回同一值
                cwd = "/" + project_dir.name.lstrip("-").replace("-", "/")
                origin = cwd
            elif not cwd:
                # 只有 cwd 空、origin 有值時，cwd fallback 到 origin
                cwd = origin
            elif not origin:
                # 只有 origin 空時，退回 cwd（開場 == 當下）
                origin = cwd
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
                    provider="claude-code",
                    _tail_kind=_conversation_tail_kind(records),
                    origin_cwd=origin,
                )
            )

    # 按「當下 cwd」（s.cwd）分組——確保 liveness 排名母體與計數母體一致。
    # counts / cwd_ttys 的鍵是 live process 回報的當下 cwd，分組鍵必須相同才能正確比對。
    # 每個 cwd 群組裡，mtime 最新的 N 個＝活著（N 取決於該 cwd 的 live claude 數）。
    # 另外：working 門檻內還在寫的，無論如何算活著（cwd 比對失敗時的保險）。
    #
    # 注意：「此 session 屬於哪個專案」由 Session.project property 讀 origin_cwd 獨立處理，
    # 與這裡的 liveness 分組無關——兩者語意已分離，不需要讓分組鍵跟著改。
    by_cwd: dict[str, list[Session]] = {}
    for s in raw:
        by_cwd.setdefault(s.cwd, []).append(s)
    out: list[Session] = []
    for _cwd, group in by_cwd.items():
        group.sort(key=lambda s: s.last_active, reverse=True)
        # 同一 cwd 組內，各 session 依自己的當下 cwd 查 live 名額與 tty
        for i, s in enumerate(group):
            live_n = counts.get(s.cwd, 0)
            # 當下 cwd 只有一個 claude 時，把它的 tty 給那個活著的 session（終端跳轉用）；
            # 多個 claude 同 cwd 無法精準對應，留給 hook 模式處理。
            uniq_tty = cwd_ttys[s.cwd][0] if live_n == 1 and cwd_ttys.get(s.cwd) else ""
            idle = now - s.last_active
            if i < live_n or idle < WORKING_THRESHOLD_SECONDS:
                s.status = _scan_status(idle)
                s.status = _apply_waiting(s.status, idle, s._tail_kind, WAITING_WINDOW_SECONDS)
            if i == 0 and uniq_tty:
                s.tty = uniq_tty
            out.append(s)
    return out


def _synthetic_sessions(procs: list[tuple[str, str]], existing: list[Session]) -> list[Session]:
    """對「有 live process 卻無任何對應 row（scan + hook 都沒有）」的 cwd 合成最小資訊列。

    :param procs:    每個還活著的 claude：(cwd, tty)，來自 ``_claude_procs()``。
    :param existing: 已由 hook + scan 產出的 session 列表（用來計算差集）。
    :returns:        補列清單（每個入選 cwd 各一列，source="proc"）。
    """
    existing_cwds = {s.cwd for s in existing}
    # 先收集每個 cwd 的所有 tty（保留順序），以便取「第一個非空 tty」
    cwd_ttys: dict[str, list[str]] = {}
    for cwd, tty in procs:
        if not cwd:
            continue
        cwd_ttys.setdefault(cwd, []).append(tty)

    out: list[Session] = []
    seen: set[str] = set()
    for cwd, tty in procs:
        if not cwd:  # _pid_cwd 失敗的情況，沒 cwd 撐不起一列
            continue
        if cwd in existing_cwds:  # 已經有 row 了（hook 或 scan 覆蓋）
            continue
        if cwd in seen:  # 同 cwd 多 process 只補一列
            continue
        seen.add(cwd)
        # 取第一個非空 tty
        first_tty = next((t for t in cwd_ttys.get(cwd, []) if t), None)
        out.append(
            Session(
                session_id=f"synthetic:{cwd}",
                cwd=cwd,
                status=Status.IDLE,
                last_active=time.time(),
                last_action="—",
                source="proc",
                tty=first_tty,
                provider="claude-code",
                origin_cwd=cwd,  # synthetic 列自身就是開場，origin == 當下
            )
        )
    return out


def _hook_sessions(
    procs: list[tuple[str, str]] | None = None,
    *,
    procs_by_provider: dict[str, list[tuple[str, str]]] | None = None,
) -> list[Session]:
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
            provider = str(data.get("provider", "claude-code") or "claude-code")
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
                    provider=provider,
                    origin_cwd=str(data.get("origin_cwd", "")),
                )
            )
        except (KeyError, ValueError):
            continue
    # SessionEnd 沒觸發（crash）會留下幽靈檔。
    # 先用 tty 精準判斷：同一 cwd 可能還有另一個 live claude，但舊 session 的 tty
    # 已不存在，不能只因 cwd 還活著就保留舊 hook row。沒有 tty 資訊時才退回 cwd 判斷。
    if out:
        for s in out:
            if s.provider not in {"claude", "claude-code", "codex"}:
                continue
            provider_procs = procs_by_provider.get(s.provider, []) if procs_by_provider is not None else (procs or [])
            if s.provider == "claude":
                provider_procs = (
                    procs_by_provider.get("claude-code", []) if procs_by_provider is not None else provider_procs
                )
            counts: dict[str, int] = {}
            live_ttys: dict[str, set[str]] = {}
            for cwd, tty in provider_procs:
                counts[cwd] = counts.get(cwd, 0) + 1
                if tty:
                    live_ttys.setdefault(cwd, set()).add(tty)
            if counts.get(s.cwd, 0) == 0:
                s.status = Status.ENDED
            elif s.tty and live_ttys.get(s.cwd) and s.tty not in live_ttys[s.cwd]:
                s.status = Status.ENDED
    return out
