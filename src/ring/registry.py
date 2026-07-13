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

import fcntl
import json
import os
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ring.config import get_config
from ring.transcript import (
    _conversation_tail_kind,
    _extract_todo,
    _head_cwd,
    _latest_action,
    _recent_actions,
    _tail_records,
)

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
RING_REGISTRY = Path.home() / ".config" / "ring" / "sessions"
DELETED_SESSIONS = Path.home() / ".config" / "ring" / "deleted_sessions.json"
CODEX_STATE = Path.home() / ".codex" / "state_5.sqlite"

_CFG = get_config()
ACTIVE_WINDOW_SECONDS = _CFG.active_window_seconds  # 只看最近這段時間動過的 session（預設 6h）
WORKING_THRESHOLD_SECONDS = _CFG.working_threshold_seconds  # 多久沒動 → 🟢 工作中 變 🟡 閒置
WAITING_WINDOW_SECONDS = _CFG.waiting_window_seconds  # IDLE 升 WAITING 的時間窗上限（預設 30 分）
_SUBPROCESS_CACHE_TTL = 1.0  # ps / tmux 結果的短快取，省掉同一次刷新內的重複呼叫

# Claude Code SessionStart payload 的 source 值（不是 provider）。舊版 bug 曾把它誤當
# provider 寫進 registry，留下接不住的幽靈列；載入時據此辨識並清掉這種腐壞檔。
_SESSION_START_SOURCES = {"startup", "resume", "clear", "compact"}
WAITING_KIND_ICONS = {
    "permission": "🔐",
    "question": "❓",
    "plan": "🧭",
    "idle": "⏸",
}
HOOK_HEARTBEAT_STALE_GRACE_SECONDS = 60.0

# Provider → 「當下 live process 的 (cwd, tty) 清單」偵測器。core 不認識任何具體工具：
# 要支援新工具的存活偵測＝註冊一個偵測器，_hook_sessions / sources 零改動。
# 同義 provider 名先正規化（例如 "claude" → "claude-code"）。
_PROVIDER_ALIASES: dict[str, str] = {"claude": "claude-code"}
_PROVIDER_PROCS: dict[str, Callable[[], list[tuple[str, str]]]] = {}


def _canonical_provider(provider: str) -> str:
    """把同義 provider 名收斂成偵測器註冊用的標準鍵。"""
    return _PROVIDER_ALIASES.get(provider, provider)


def _session_registry_path(session_id: str) -> Path:
    """RiNG hook registry 裡某 session 對應的狀態檔路徑。"""
    return RING_REGISTRY / f"{quote(session_id, safe=':')}.json"


def delete_session_state(session_id: str) -> bool:
    """刪除 RiNG 自己保存的單一 session 狀態檔。

    這只處理 ``~/.config/ring/sessions`` 底下由 hook 寫出的 registry；不碰
    Claude Code JSONL、Codex SQLite state 或其他 provider 的原始資料。回傳值表示是否
    真的刪到檔案。
    """
    direct = _session_registry_path(session_id)
    try:
        if direct.exists():
            direct.unlink()
            return True
    except OSError:
        return False

    # 向後相容：若未來/舊版 filename quote 規則不同，仍用檔內 session_id 找一次。
    if not RING_REGISTRY.is_dir():
        return False
    for path in RING_REGISTRY.glob("*.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if str(data.get("session_id", "")) != session_id:
            continue
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _epoch_to_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).isoformat()


def _parse_hidden_at(value: object) -> float | None:
    """把 deleted_sessions.json 裡一筆 hidden_at 轉成 epoch 秒，供跟 last_active 比較。"""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).timestamp()
        except ValueError:
            return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


@contextmanager
def _hidden_sessions_lock(path: Path) -> Iterator[None]:
    """跨 process 的 read-modify-write 臨界區，保護 deleted_sessions.json 不 lost-update。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(path.name + ".lock")
    with open(lock_path, "w", encoding="utf-8") as fh:
        fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def _read_hidden_sessions_locked(path: Path) -> dict[str, float]:
    """讀 deleted_sessions.json，回傳 ``{session_id: hidden_at}``（epoch 秒）。

    容忍舊格式（純 id 列表）：就地遷移成新格式（value 是遷移當下的 ISO
    timestamp）並立刻寫回，之後都是新格式。呼叫端必須已持有 ``_hidden_sessions_lock``。
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if isinstance(raw, list):
        migrated_iso = {str(sid): _now_iso() for sid in raw if isinstance(sid, str) and sid}
        _write_hidden_sessions_locked(migrated_iso, path=path)
        return {sid: _parse_hidden_at(ts) or 0.0 for sid, ts in migrated_iso.items()}

    if not isinstance(raw, dict):
        return {}

    result: dict[str, float] = {}
    for sid, value in raw.items():
        if not isinstance(sid, str) or not sid:
            continue
        ts = _parse_hidden_at(value)
        if ts is not None:
            result[sid] = ts
    return result


def _write_hidden_sessions_locked(iso_by_id: dict[str, str], *, path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(iso_by_id, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def hidden_sessions(*, path: Path | None = None) -> dict[str, float]:
    """讀取手動隱藏的 session id 與隱藏時間（epoch 秒）。

    給需要跟 ``Session.last_active`` 比較、判斷「有新活動就自動復活」的呼叫端用。
    """
    p = path or DELETED_SESSIONS
    with _hidden_sessions_lock(p):
        return _read_hidden_sessions_locked(p)


def hidden_session_ids(*, path: Path | None = None) -> set[str]:
    """讀取使用者手動從看板隱藏的 session id（只要 id 集合時用這個）。"""
    return set(hidden_sessions(path=path).keys())


def hide_session(session_id: str, *, path: Path | None = None) -> None:
    """把 session 加入手動隱藏清單；用於 dashboard 的 ``dd``。"""
    p = path or DELETED_SESSIONS
    with _hidden_sessions_lock(p):
        hidden = _read_hidden_sessions_locked(p)
        hidden[session_id] = time.time()
        _write_hidden_sessions_locked({sid: _epoch_to_iso(ts) for sid, ts in hidden.items()}, path=p)


def unhide_session(session_id: str, *, path: Path | None = None) -> None:
    """新的 hook 事件、或偵測到更新活動，代表 session 又活了，解除手動隱藏。"""
    p = path or DELETED_SESSIONS
    with _hidden_sessions_lock(p):
        hidden = _read_hidden_sessions_locked(p)
        if session_id not in hidden:
            return
        del hidden[session_id]
        _write_hidden_sessions_locked({sid: _epoch_to_iso(ts) for sid, ts in hidden.items()}, path=p)


def prune_hidden_sessions(
    *,
    known_ids: set[str] | None,
    older_than: float,
    now: float | None = None,
    path: Path | None = None,
) -> dict[str, float]:
    """清掉隱藏清單裡「任何來源都找不到」或超過保留期的條目。供 ``ring gc`` 用。

    ``known_ids`` 是目前所有來源仍找得到的 session id；``None`` 時只套用保留期，
    不做「找不到」判斷。回傳被清掉的 ``{session_id: hidden_at}``（epoch 秒）。
    """
    current = time.time() if now is None else now
    p = path or DELETED_SESSIONS
    with _hidden_sessions_lock(p):
        hidden = _read_hidden_sessions_locked(p)
        stale: dict[str, float] = {}
        keep: dict[str, float] = {}
        for sid, hidden_at in hidden.items():
            not_found = known_ids is not None and sid not in known_ids
            too_old = current - hidden_at >= older_than
            if not_found or too_old:
                stale[sid] = hidden_at
            else:
                keep[sid] = hidden_at
        if stale:
            _write_hidden_sessions_locked({sid: _epoch_to_iso(ts) for sid, ts in keep.items()}, path=p)
        return stale


def register_provider_procs(provider: str, detector: Callable[[], list[tuple[str, str]]]) -> None:
    """註冊某 provider 的 live-process 偵測器（回傳 ``[(cwd, tty), …]``）。

    有偵測器的 provider 才會在 ``_hook_sessions`` 走 process-based 存活清理；沒註冊的
    provider 一律 fail-open（不靠 process 判離場，交給該工具自己的 SessionEnd hook）。
    """
    _PROVIDER_PROCS[_canonical_provider(provider)] = detector


def collect_provider_procs() -> dict[str, list[tuple[str, str]]]:
    """所有已註冊 provider 的當下 live procs，鍵為標準 provider 名。"""
    return {provider: detector() for provider, detector in _PROVIDER_PROCS.items()}


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
    tmux_pane: str | None = None  # stable tmux pane id from hook, e.g. "%12"
    tty: str | None = None  # e.g. "/dev/ttys003"，給非-tmux 終端（iTerm2 等）聚焦用
    hook_pid: int | None = None
    heartbeat_at: float = 0.0
    source_path: str = ""
    hook_stale: bool = False
    todo: tuple[int, int] | None = None  # (done, total)
    recent_actions: list[str] = field(default_factory=list)
    provider: str = ""
    waiting_kind: str = ""  # permission | question | plan | idle；空代表非 WAITING 或舊 registry
    waiting_detail: str = ""  # 🔴 等你時「到底在等什麼」（權限指令 / 問題內容；hook 模式才有）
    kind: str = "foreground"  # "foreground" | "agent"；背景 agent（bg-pty-host 承載）由 discover 貼標
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

    @property
    def waiting_icon(self) -> str:
        return WAITING_KIND_ICONS.get(self.waiting_kind, "")


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


def _hook_heartbeat_stale(
    source_path: str,
    heartbeat_at: float,
    status: Status,
    *,
    grace_seconds: float = HOOK_HEARTBEAT_STALE_GRACE_SECONDS,
) -> bool:
    """來源檔有更新但 hook heartbeat 沒跟上時，才視為 hook 可能失效。"""
    if status not in {Status.WAITING, Status.WORKING}:
        return False
    if not source_path or heartbeat_at <= 0:
        return False
    try:
        source_mtime = Path(source_path).stat().st_mtime
    except OSError:
        return False
    return source_mtime - heartbeat_at > grace_seconds


_tmux_cache: tuple[float, dict[str, str]] = (-1.0, {})
_tmux_panes_cache: tuple[float, list[TmuxPane]] = (-1.0, [])


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
    if 0.0 <= now - _tmux_panes_cache[0] <= _SUBPROCESS_CACHE_TTL:
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
    if 0.0 <= now - _tmux_cache[0] <= _SUBPROCESS_CACHE_TTL:
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
    """pid → (ppid, args)。給 scan-only tmux pane process-tree 消歧用。"""
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


_pids_cache: tuple[float, list[int]] = (-1.0, [])
_codex_pids_cache: tuple[float, list[int]] = (-1.0, [])
_ps_claude_snapshot_cache: tuple[float, str] = (-1.0, "")
_bg_agent_session_ids_cache: tuple[float, frozenset[str]] = (-1.0, frozenset())

# args 內任一出現即可判定「這是 claude 安裝二進位在跑」的路徑標記。ps comm 對
# daemon-exec 的二進位常被截斷（如 `/Users/weilee/.l`），單看 comm 不可靠。
_CLAUDE_PATH_MARKERS = ("ClaudeCode.app", "claude/versions/", "/.claude/")


def _is_claude_session_line(comm: str, args: str) -> bool:
    """判定一行 ``ps`` 輸出是否為 claude session process（承載者或子行程皆算）。

    comm basename 為 ``claude`` 是最常見的情況；但 daemon 承載的 process，ps comm
    會被截斷成本機路徑片段（例如 ``/Users/weilee/.l``），此時改看 args 是否含可辨識
    的 claude 安裝路徑標記，或 args 內任一 token 的 basename 為 ``claude``
    （args 首 token 有時只是版本號如 ``2.1.187``，不能只看 args[0]）。第三個 fallback
    另外要求 args 內必須有 ``--session-id``，否則像 ``grep -r claude .``、
    ``less claude`` 這類完全無關但恰好帶 ``claude`` 字面的 process 會被誤收；
    真正被截斷 comm 的 claude session（daemon 承載者與其子行程）必然帶
    ``--session-id``，所以這個限定不會犧牲 fallback 能力。
    """
    if os.path.basename(comm.strip()) == "claude":
        return True
    if any(marker in args for marker in _CLAUDE_PATH_MARKERS):
        return True
    tokens = args.split()
    if "--session-id" not in tokens:
        return False
    return any(os.path.basename(tok) == "claude" for tok in tokens)


def _ps_claude_snapshot() -> str:
    """``ps -Ao pid,comm,args`` 的短快取原始輸出，供多個 claude proc 判定函式共用。"""
    global _ps_claude_snapshot_cache
    now = time.monotonic()
    if 0.0 <= now - _ps_claude_snapshot_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _ps_claude_snapshot_cache[1]
    try:
        out = subprocess.run(["ps", "-Ao", "pid,comm,args"], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        out = ""
    _ps_claude_snapshot_cache = (now, out)
    return out


def _parse_ps_claude_lines(out: str) -> list[tuple[int, str, bool]]:
    """把 ``ps`` 輸出解析成 claude session 行：``(pid, args, is_bg_pty_host)``。

    不論是否為背景 process（daemon / bg-spare / bg-pty-host）都收進來——背景判定
    交給呼叫端各自決定要不要濾除；``_hook_sessions`` 的活性判定與
    ``running_claude_pids`` 對「該濾誰」的答案不同，不能在這裡先幫忙決定。
    """
    entries: list[tuple[int, str, bool]] = []
    for line in out.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        comm = parts[1].strip()
        args = parts[2] if len(parts) == 3 else ""
        if not _is_claude_session_line(comm, args):
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        entries.append((pid, args, "--bg-pty-host" in args.split()))
    return entries


def _arg_session_id(args: str) -> str | None:
    """解析 args 裡 ``--session-id`` 後面的那個 token；沒有就回 ``None``。"""
    tokens = args.split()
    for i, tok in enumerate(tokens):
        if tok == "--session-id" and i + 1 < len(tokens):
            return tokens[i + 1]
    return None


def running_claude_pids() -> list[int]:
    """目前活著、使用者可聚焦的 claude CLI pid（daemon / bg-spare / bg 暖機承載者濾除）。

    承載者（``--bg-pty-host`` + ``--session-id``）與其子行程常成對出現、共用同一個
    session-id：兩者都算「真 session」不濾除，但只留一個 pid，偏好子行程——子行程
    的 cwd（lsof 量得到）誠實，承載者的 cwd 常是 daemon 自己的 cwd，非專案目錄。
    只有承載者、沒有子行程時（fallback）仍保留承載者這個 pid，好過整個 session 消失。
    """
    global _pids_cache
    now = time.monotonic()
    if 0.0 <= now - _pids_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _pids_cache[1]
    entries = _parse_ps_claude_lines(_ps_claude_snapshot())

    pids: list[int] = []
    sid_index: dict[str, int] = {}  # session-id → 該 pid 在 pids 裡的位置，供子行程晚到時換掉
    sid_is_bg_host: dict[str, bool] = {}
    for pid, args, is_bg_host in entries:
        if _is_claude_background_process(args):
            continue
        session_id = _arg_session_id(args)
        if session_id is None:
            pids.append(pid)
            continue
        if session_id not in sid_index:
            sid_index[session_id] = len(pids)
            sid_is_bg_host[session_id] = is_bg_host
            pids.append(pid)
        elif sid_is_bg_host[session_id] and not is_bg_host:
            # 子行程晚到：換掉先記到的承載者 pid，偏好子行程（cwd 誠實）。
            pids[sid_index[session_id]] = pid
            sid_is_bg_host[session_id] = False

    _pids_cache = (now, pids)
    return pids


def background_agent_session_ids() -> set[str]:
    """所有背景 agent（``--bg-pty-host`` 承載且已載入真 session）的 session-id 集合。

    給 ``discover_sessions()`` 對應貼 ``kind="agent"`` 標籤用。與 ``running_claude_pids``
    共用同一份 ``ps`` 快照（``_ps_claude_snapshot``），不額外多打一次 ``ps``。
    """
    global _bg_agent_session_ids_cache
    now = time.monotonic()
    if 0.0 <= now - _bg_agent_session_ids_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return set(_bg_agent_session_ids_cache[1])
    entries = _parse_ps_claude_lines(_ps_claude_snapshot())
    ids = frozenset(
        session_id
        for _pid, args, is_bg_host in entries
        if is_bg_host and (session_id := _arg_session_id(args)) is not None
    )
    _bg_agent_session_ids_cache = (now, ids)
    return set(ids)


def _is_claude_background_process(args: str) -> bool:
    """Claude daemon / bg pty host 暖機承載者 / bg-spare 不是使用者可聚焦的 CLI session。

    ``--bg-spare`` 是 Claude Code 預熱的備用 process（供下一個 ``claude`` 呼叫快速接手），
    跟尚未載入真 session 的 ``--bg-pty-host`` 承載者一樣不代表真正的使用者 session，卻會
    被 ``_claude_procs`` 合成假 session 列上看板，冒出幽靈列。token 形狀（`--bg-spare`，
    `--` flag，不是位置參數）取自本機 claude CLI 2.1.206 二進位的 strings 掃描（無法用
    ``ps`` 現場逮到活體 bg-spare process，掃描時機是巧合——它壽命短、隨用隨滅）：
    ``[a,...l,"--bg-pty-host",r,"200","50","--",a,...l,"--bg-spare",n]`` spawn 呼叫，
    以及 bg-spare process 自己啟動時對 ``process.argv`` 做的
    ``e.includes("--bg-spare", t+1)`` 檢查，兩處都證實是 ``--`` 前綴的 flag token。

    ``--bg-pty-host`` 本身不再一律濾除：暖機階段（spare sock，無 ``--session-id``）仍
    濾除；一旦掛上真正的 ``--session-id``（使用者已進入 agents、真背景 session），就不
    再視為背景 process——那是一個真人在跑的背景 agent，該讓它現身，只是要標成
    ``kind="agent"``（見 ``background_agent_session_ids``）。
    """
    tokens = args.split()
    if len(tokens) >= 3 and tokens[1:3] == ["daemon", "run"]:
        return True
    if "--bg-spare" in tokens:
        return True
    if "--bg-pty-host" in tokens:
        return _arg_session_id(args) is None
    return False


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


# 內建 provider 的 live-process 偵測器。外部工具用 register_provider_procs() 加自己的。
register_provider_procs("claude-code", _claude_procs)
register_provider_procs("codex", _codex_procs)


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
        key = _real(cwd)
        counts[key] = counts.get(key, 0) + 1
        cwd_ttys.setdefault(key, []).append(tty)

    raw: list[Session] = []
    for row in rows:
        cwd = str(row["cwd"] or "")
        if not cwd:
            continue
        updated_ms = int(row["updated_at_ms"] or 0)
        last_active = updated_ms / 1000 if updated_ms else float(row["updated_at"] or 0)
        if now - last_active > ACTIVE_WINDOW_SECONDS and counts.get(_real(cwd), 0) == 0:
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
        ckey = _real(cwd)
        live_n = counts.get(ckey, 0)
        uniq_tty = cwd_ttys[ckey][0] if live_n == 1 and cwd_ttys.get(ckey) else ""
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
        key = _real(cwd)
        counts[key] = counts.get(key, 0) + 1
        cwd_ttys.setdefault(key, []).append(tty)

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
    # 沒有 live process 對上的 transcript 直接維持 ENDED；若真的有活 process 但 cwd 對不上，
    # _synthetic_sessions 會補一列 source="proc"，不要讓舊 transcript 冒充活 session。
    #
    # 注意：「此 session 屬於哪個專案」由 Session.project property 讀 origin_cwd 獨立處理，
    # 與這裡的 liveness 分組無關——兩者語意已分離，不需要讓分組鍵跟著改。
    by_cwd: dict[str, list[Session]] = {}
    for s in raw:
        by_cwd.setdefault(s.cwd, []).append(s)
    out: list[Session] = []
    for _cwd, group in by_cwd.items():
        group.sort(key=lambda s: s.last_active, reverse=True)
        skey = _real(group[0].cwd)  # 同一 group 內 cwd 皆相同（by_cwd 就是照 s.cwd 分組）
        live_n = counts.get(skey, 0)
        ordered = group
        if 0 < live_n < len(group):
            # 曖昧情境：同 cwd 的 transcript 數多於 live claude 數，純 mtime 排名不可靠
            # ——已崩潰的 session 若剛好在真正還活著、但已安靜一段時間的 session 之後
            # 才寫入最後一筆，mtime 反而「更新」，會把真正活著的那個擠出 live_n 名額
            # （見 session-detection-review.md 症狀 1）。若能從 tmux pane 子孫 process
            # 的 args 找到明確提到 session id 的強訊號（比照 _tmux_process_tree_targets
            # 用在 tmux_target 配對的同一套邏輯），優先信任它決定誰佔 live 名額；沒有這
            # 種訊號（多數非 tmux／非 --resume 情境）就 fallback 回既有 mtime 排名。
            confirmed = _tmux_process_tree_targets(group)
            if confirmed:
                front = [s for s in group if s.session_id in confirmed]
                back = [s for s in group if s.session_id not in confirmed]
                ordered = front + back
        # 同一 cwd 組內，各 session 依自己的當下 cwd 查 live 名額與 tty
        for i, s in enumerate(ordered):
            # 當下 cwd 只有一個 claude 時，把它的 tty 給那個活著的 session（終端跳轉用）；
            # 多個 claude 同 cwd 無法精準對應，留給 hook 模式處理。
            uniq_tty = cwd_ttys[skey][0] if live_n == 1 and cwd_ttys.get(skey) else ""
            idle = now - s.last_active
            if i < live_n:
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
    existing_cwds = {_real(s.cwd) for s in existing}
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
        rkey = _real(cwd)
        if rkey in existing_cwds:  # 已經有 row 了（hook 或 scan 覆蓋）
            continue
        if rkey in seen:  # 同 cwd 多 process 只補一列
            continue
        seen.add(rkey)
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
    purge_session_start_phantoms: bool = True,
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
            if provider in _SESSION_START_SOURCES:
                # 舊版 bug 把 SessionStart 的 source（startup/resume/clear/compact）誤當
                # provider，留下無 tty、跳不過去、又永不離場的幽靈列。清掉這種腐壞檔，自我修復。
                if purge_session_start_phantoms:
                    f.unlink(missing_ok=True)
                continue
            out.append(
                Session(
                    session_id=str(data["session_id"]),
                    cwd=str(data.get("cwd", "")),
                    status=Status(data.get("status", "idle")),
                    last_active=float(data.get("last_active", 0.0)),
                    last_action=str(data.get("last_action", "—")),
                    source="hook",
                    tmux_pane=str(data.get("tmux_pane", "")) or None,
                    tty=str(data.get("tty", "")) or None,
                    hook_pid=int(data["hook_pid"]) if str(data.get("hook_pid", "")).isdigit() else None,
                    heartbeat_at=float(data.get("heartbeat_at", data.get("last_active", 0.0))),
                    source_path=str(data.get("source_path", "")),
                    todo=tuple(todo) if isinstance(todo, list) and len(todo) == 2 else None,
                    provider=provider,
                    waiting_kind=str(data.get("waiting_kind", "")),
                    waiting_detail=str(data.get("waiting_detail", "")),
                    origin_cwd=str(data.get("origin_cwd", "")),
                )
            )
        except (KeyError, ValueError):
            continue
    for s in out:
        if s.source == "hook":
            s.hook_stale = _hook_heartbeat_stale(s.source_path, s.heartbeat_at, s.status)
    # SessionEnd 沒觸發（crash）會留下幽靈檔。判定離場：
    #   1. 該 cwd 完全沒有 live proc → 一定離場。
    #   2. 該 cwd 的 hook row 數「多於一筆」時，用 tty 挑出 tty 對不上的那幾筆標離場——
    #      不論這筆數是否 <= live proc 數：計數只是巧合對上（例如同 cwd 剛好有跟 RiNG
    #      hook 無關的 live process 佔掉名額），不代表每筆 row 都真的還活著；row 數 > 1
    #      時 tty 交叉比對才有意義去挑出誰是 stale 的。
    #   3. 該 cwd 只有「單一」hook row 時，無論 tty 是否對得上都不靠 tty 殺——hook 寫進來
    #      的 tty 不一定可靠（終端 tty 會被作業系統重配，甚至跨 session 錯置），拿它隱藏
    #      唯一活著的 session 會讓整列憑空消失。
    if out:
        proc_counts: dict[tuple[str, str], int] = {}
        proc_ttys: dict[tuple[str, str], set[str]] = {}
        proc_cwds_by_provider: dict[str, list[str]] = {}
        for pk in _PROVIDER_PROCS:
            provider_procs = procs_by_provider.get(pk, []) if procs_by_provider is not None else (procs or [])
            for cwd, tty in provider_procs:
                real_cwd = _real(cwd)
                key = (pk, real_cwd)
                proc_counts[key] = proc_counts.get(key, 0) + 1
                if tty:
                    proc_ttys.setdefault(key, set()).add(tty)
                proc_cwds_by_provider.setdefault(pk, []).append(real_cwd)

        rows_by_key: dict[tuple[str, str], list[Session]] = {}
        for s in out:
            pk = _canonical_provider(s.provider)
            if pk not in _PROVIDER_PROCS:
                continue  # 沒有 proc 偵測器 → 無法驗活性 → fail-open，交給 SessionEnd
            rows_by_key.setdefault((pk, _real(s.cwd)), []).append(s)

        for key, rows in rows_by_key.items():
            live_n = proc_counts.get(key, 0)
            if live_n == 0:
                pk, row_cwd = key
                if _has_ancestor_live_process(row_cwd, proc_cwds_by_provider.get(pk, [])):
                    # hook payload 的 cwd 落在使用者 cd 進去的子目錄，但 claude process 實際
                    # cwd（lsof 量到的）仍停在啟動目錄——兩者都正規化過，子目錄底下自然量不到
                    # live proc。祖先目錄有活 process 時保守判定「還活著」，不殺，避免把正常
                    # cd 進子目錄的 session 誤判 ENDED（見
                    # test_hook_sessions_keeps_live_session_when_cwd_moved_to_subdir）。
                    continue
                for s in rows:
                    s.status = Status.ENDED
                continue

            # 單筆 row：不靠 tty 殺，理由見上方註解 3——避免唯一活著的 session 因 tty
            # 重配而憑空消失（test_hook_sessions_keeps_lone_live_session_with_wrong_tty）。
            if len(rows) == 1:
                continue

            live_ttys = proc_ttys.get(key, set())
            if live_ttys:
                for s in rows:
                    if s.tty and s.tty not in live_ttys:
                        s.status = Status.ENDED

            if len(rows) <= live_n:
                # 沒有多餘列要修剪，但上面的 tty 交叉比對仍然有效——即使計數巧合對上，
                # tty 對不上的那幾筆（例如已 crash 的舊 row）還是會被標離場，不會永遠
                # 靠計數巧合躲過清理。
                continue

            remaining = [s for s in rows if s.status is not Status.ENDED]
            if len(remaining) > live_n:
                remaining.sort(key=lambda s: s.last_active, reverse=True)
                for s in remaining[live_n:]:
                    s.status = Status.ENDED
    return out
