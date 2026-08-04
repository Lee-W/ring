"""RiNG 資料層：把「目前有哪些 Claude Code session 在台上」抓出來。

兩種來源，優先順序由高到低：

1. hook registry（精準模式）：``~/.config/ring/sessions/*.json``，由 RiNG 的 hook
   腳本在 SessionStart / Notification / UserPromptSubmit / Stop / SessionEnd
   等事件即時寫入。能精準知道「這個 session 需要你決策」。
2. zero-config fallback：直接掃 ``~/.claude/projects/**/*.jsonl``，用檔案 mtime
   推活躍度，從記錄裡的 ``cwd`` 欄位還原真實路徑（避開目錄名以 ``-`` 編碼
   造成的 hyphen 還原歧義）。scan 模式不把「回完一輪」當成 🔴 WAITING；
   WAITING 一律由 hook 資料驅動：hook 事件直接標的權限 / 選項互動，加上
   codex 的核可等待靜默逾時判定（``_promote_codex_permission_wait``——它讀的
   也是 hook row，是推遲判定而非 scan 猜測）。

額外富化：
- ``tmux_target``：靠 tmux pane 的 current_path 對 cwd，給你「去哪」的座標。
- ``todo``：解析 transcript 裡最新的 TodoWrite，給 done/total 真進度。

純 stdlib，不依賴任何第三方套件。
"""

from __future__ import annotations

import fcntl
import json
import sqlite3
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ring.config import get_config
from ring.pathutil import _has_ancestor_live_process, _real
from ring.ps_parse import (
    _arg_session_id,
    _is_claude_background_process,
    _is_codex_internal_process,
    _normalize_tty,
    _parse_ps_claude_lines,
    _parse_ps_codex_lines,
)
from ring.subproc import CACHE_TTL as _SUBPROCESS_CACHE_TTL
from ring.tmux_scan import _tmux_process_tree_targets
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
WAITING_WINDOW_SECONDS = _CFG.waiting_window_seconds  # 近期 end_turn scan row 收斂成 IDLE 的時間窗
# codex 裸 PermissionRequest 後 hook 靜默超過這秒數 → 判定真的停下來等核可（0 = 關閉）
CODEX_PERMISSION_WAIT_SECONDS = _CFG.codex_permission_wait_seconds

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
# 只送過 SessionStart 就再無下文的 hook row，寬限多久後判離場（見 _is_bare_session_start_row）
BARE_SESSION_START_GRACE_SECONDS = 120.0

# Provider → 「當下 live process 的 (cwd, tty) 清單」偵測器。core 不認識任何具體工具：
# 要支援新工具的存活偵測＝註冊一個偵測器，_hook_sessions / sources 零改動。
# 同義 provider 名先正規化（例如 "claude" → "claude-code"）。
_PROVIDER_ALIASES: dict[str, str] = {"claude": "claude-code"}
_PROVIDER_PROCS: dict[str, Callable[[], list[tuple[str, str]] | None]] = {}


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
        except OSError:
            return False
        else:
            return True
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
    with lock_path.open("w", encoding="utf-8") as fh:
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


def register_provider_procs(provider: str, detector: Callable[[], list[tuple[str, str]] | None]) -> None:
    """註冊某 provider 的 live-process 偵測器（回傳 ``[(cwd, tty), …]``）。

    有偵測器的 provider 才會在 ``_hook_sessions`` 走 process-based 存活清理；沒註冊的
    provider 一律 fail-open（不靠 process 判離場，交給該工具自己的 SessionEnd hook）。
    """
    _PROVIDER_PROCS[_canonical_provider(provider)] = detector


def collect_provider_procs() -> dict[str, list[tuple[str, str]] | None]:
    """所有已註冊 provider 的當下 live procs，鍵為標準 provider 名。

    值為 ``None`` 代表該 provider 這輪偵測失敗（未知），呼叫端（``_hook_sessions``）
    必須把它與「真的偵測到零個 live process」分開處理，不能兩者都判離場。
    """
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
    # 合流彙總通知（見 ring.notify.notify_summary）用的標記：True 代表這不是真實 session，
    # 只是借一個「真實 session 的欄位」組出來的通知 payload，讓 notify_title / notify_message
    # 改用彙總句式。session_id 本身維持真實值（不覆寫成 sentinel），點擊通知才能正確 focus。
    is_summary: bool = field(default=False, repr=False, compare=False)

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
    不把回合結束升成 WAITING；WAITING 一律由 hook 資料驅動（hook 直接標的權限 /
    選項互動，或 ``_promote_codex_permission_wait`` 對 hook row 的靜默逾時判定），
    scan 猜測永遠不標：
    - WORKING（< 90s）：若尾端已是 end_turn，代表回合其實結束了，收斂成 IDLE。
    - ENDED：超過活躍窗，不升。
    """
    if status in {Status.WORKING, Status.IDLE} and tail_kind == "waiting" and idle_seconds < waiting_window:
        return Status.IDLE
    return status


def _promote_codex_permission_wait(
    provider: str,
    status: Status,
    last_event: str,
    age_seconds: float,
    threshold: float,
) -> bool:
    """codex hook row 的核可等待判定：裸 PermissionRequest 後靜默逾時 → 該升 🔴 嗎。

    Codex（0.144.4 實證）的 hook 是封閉的 10 事件枚舉：沒有「使用者已核可」事件、沒有
    心跳，rollout 檔在等核可期間也完全靜默。policy 自動放行時，下一個事件（PostToolUse /
    下一個 PreToolUse / Stop）幾秒內就會到；真的停下來等人時 hook 通道只會一直沉默。
    所以「最後一個 hook 事件是 PermissionRequest 且已靜默超過門檻」本身就是可靠的等待
    訊號——這是對 hook 資料的推遲判定，不是 scan 猜測。任何後續 hook 事件會覆寫
    last_event，自然清紅。

    只對 codex 啟用：claude-code 真的停下來等人時「通常」會補發 permission_prompt
    Notification（hook 直接標 🔴），不需要、也不該重複走這條路。純函式、可單測。

    已知例外（2026-07-20 現場取證）：claude-code 少數未記載的 UI guardrail——例如
    「Multiple directory changes in one command require approval」（單一 Bash 指令內
    含多個 cd）——阻塞等核可時**完全不 fire 任何 hook**（PreToolUse / PermissionRequest
    / permission_prompt 皆無，raw payload log 掛零可證）。這類提示走在 hook lifecycle
    之外，RiNG 無法可靠偵測；官方也無非-hook 機制可查詢阻塞狀態。此限制屬上游，非本函式
    可補——刻意不把靜默逾時判定擴到 claude-code，因為它連 idle_prompt 都送、與正常閒置
    無法區分，擴了只會誤報。
    """
    return (
        provider == "codex"
        and status is Status.WORKING
        and last_event == "PermissionRequest"
        and threshold > 0
        and age_seconds > threshold
    )


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


def _is_bare_session_start_row(
    tty: str,
    last_event: str,
    age_seconds: float,
    *,
    grace_seconds: float = BARE_SESSION_START_GRACE_SECONDS,
) -> bool:
    """這筆 hook row 是不是「只送過 SessionStart 就再無下文、又沒有終端」的承載 session。

    Claude Code 的背景 job／agent（``--bg-pty-host`` / ``--agent``）起來時會送一筆
    SessionStart，之後可能整段生命週期都不再送任何事件。hook row 的狀態只靠事件推進
    （不像 scan row 會隨 idle 秒數自己衰減），所以這種 row 會以 🟢 working 卡在看板上
    直到 ``ACTIVE_WINDOW_SECONDS`` 過期，而且它沒有 tty、跳也跳不過去——2026-07-28
    現場取證：一次背景 job 讓看板憑空多一列，registry 裡最久的一筆已經掛了三天。

    真正在做事的背景 job 起步幾秒內就會送出 UserPromptSubmit／PreToolUse，寬限期過後
    仍只有 SessionStart 才判離場；之後只要它真的動起來，下一個事件會把 row 寫回在場，
    不是永久刪除。有 tty 的前景 session 一律不套用——剛開好還沒輸入的終端 session 也
    只有 SessionStart，那是正常在場狀態。

    :param tty:           row 記錄到的終端；空字串代表沒有可聚焦終端。
    :param last_event:    registry 檔裡的 ``last_event``。
    :param age_seconds:   距離 ``last_active`` 過了多久。
    :param grace_seconds: 寬限秒數，超過才判離場。
    """
    return not tty and last_event == "SessionStart" and age_seconds > grace_seconds


_pids_cache: tuple[float, list[int]] = (-1.0, [])
_codex_pids_cache: tuple[float, list[int]] = (-1.0, [])
_ps_claude_snapshot_cache: tuple[float, str] = (-1.0, "")
_ps_codex_snapshot_cache: tuple[float, str] = (-1.0, "")
_bg_agent_session_ids_cache: tuple[float, frozenset[str]] = (-1.0, frozenset())
_claude_procs_cache: tuple[float, list[tuple[str, str]]] = (-1.0, [])
_codex_procs_cache: tuple[float, list[tuple[str, str]]] = (-1.0, [])


def _ps_claude_snapshot() -> str | None:
    """``ps -Ao pid,comm,args`` 的短快取原始輸出，供多個 claude proc 判定函式共用。

    回傳 ``None`` 代表這次 ``ps`` 呼叫失敗（逾時／例外）——這是「不知道」，不是
    「系統上沒有任何 process」，呼叫端必須分開處理，不能把 ``None`` 當空字串解析出
    零筆存活 process。失敗一律不進快取，好讓下一輪立刻重試，不會被短 TTL 快取卡住。
    """
    global _ps_claude_snapshot_cache
    now = time.monotonic()
    if 0.0 <= now - _ps_claude_snapshot_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _ps_claude_snapshot_cache[1]
    try:
        result = subprocess.run(["ps", "-Ao", "pid,tty,comm,args"], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # 非零 exit 跟逾時／例外一樣是「這次沒問到」，不是「問到了、答案是沒有 process」。
        return None
    _ps_claude_snapshot_cache = (now, result.stdout)
    return result.stdout


def _claude_tty_map() -> dict[int, str] | None:
    """pid → tty，來自 ``_ps_claude_snapshot()`` 共用的快照，不再多開一次 ``ps``。

    回傳 ``None`` 代表這輪 ``ps`` 掃描失敗（未知）；成功但某 pid 沒出現在快照裡
    （已死）時，該 pid 直接不在回傳 dict 內——呼叫端用 ``.get(pid, "")`` 取值即可。
    """
    snapshot = _ps_claude_snapshot()
    if snapshot is None:
        return None
    return {pid: tty for pid, tty, _args, _is_bg_host in _parse_ps_claude_lines(snapshot)}


def running_claude_pids() -> list[int] | None:
    """目前活著、使用者可聚焦的 claude CLI pid（daemon / bg-spare / bg 暖機承載者濾除）。

    承載者（``--bg-pty-host`` + ``--session-id``）與其子行程常成對出現、共用同一個
    session-id：兩者都算「真 session」不濾除，但只留一個 pid，偏好子行程——子行程
    的 cwd（lsof 量得到）誠實，承載者的 cwd 常是 daemon 自己的 cwd，非專案目錄。
    只有承載者、沒有子行程時（fallback）仍保留承載者這個 pid，好過整個 session 消失。

    回傳 ``None`` 代表這輪 ``ps`` 掃描失敗（未知），不是「沒有任何 claude process」；
    呼叫端不得把 ``None`` 當空清單使用來判定 session 離場。失敗不快取，下一輪重試。
    """
    global _pids_cache
    now = time.monotonic()
    if 0.0 <= now - _pids_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _pids_cache[1]
    snapshot = _ps_claude_snapshot()
    if snapshot is None:
        return None
    entries = _parse_ps_claude_lines(snapshot)

    pids: list[int] = []
    sid_index: dict[str, int] = {}  # session-id → 該 pid 在 pids 裡的位置，供子行程晚到時換掉
    sid_is_bg_host: dict[str, bool] = {}
    for pid, _tty, args, is_bg_host in entries:
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


def running_foreground_claude_pids() -> list[int] | None:
    """目前仍有可聚焦終端的 Claude session pid。

    ``running_claude_pids`` 也包含 agents mode 的背景 session，因為 scan 需要靠它們
    找到對應 transcript；但 hook registry 不能拿背景 agent 的 cwd／數量替同專案的
    舊前景 row 證明存活，否則一個 agent 就可能讓 crash 數小時的 session 繼續顯示。

    回傳 ``None`` 代表這輪掃描失敗（未知），呼叫端不得當空清單處理。
    """
    bg_ids = background_agent_session_ids()
    if bg_ids is None:
        return None
    base_pids = running_claude_pids()
    if base_pids is None:
        return None
    if not bg_ids:
        return base_pids
    snapshot = _ps_claude_snapshot()
    if snapshot is None:
        return None
    args_by_pid = {pid: args for pid, _tty, args, _is_bg_host in _parse_ps_claude_lines(snapshot)}
    return [
        pid
        for pid in base_pids
        if (session_id := _arg_session_id(args_by_pid.get(pid, ""))) is None or session_id not in bg_ids
    ]


def background_agent_session_ids() -> set[str] | None:
    """所有背景 agent（``--bg-pty-host`` 承載且已載入真 session）的 session-id 集合。

    給 ``discover_sessions()`` 對應貼 ``kind="agent"`` 標籤用。與 ``running_claude_pids``
    共用同一份 ``ps`` 快照（``_ps_claude_snapshot``），不額外多打一次 ``ps``。

    回傳 ``None`` 代表這輪掃描失敗（未知），不是「沒有背景 agent」。
    """
    global _bg_agent_session_ids_cache
    now = time.monotonic()
    if 0.0 <= now - _bg_agent_session_ids_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return set(_bg_agent_session_ids_cache[1])
    snapshot = _ps_claude_snapshot()
    if snapshot is None:
        return None
    entries = _parse_ps_claude_lines(snapshot)
    ids = frozenset(
        session_id
        for _pid, _tty, args, is_bg_host in entries
        if is_bg_host and (session_id := _arg_session_id(args)) is not None
    )
    _bg_agent_session_ids_cache = (now, ids)
    return set(ids)


def _ps_codex_snapshot() -> str | None:
    """``ps -Ao pid=,tty=,comm=,args=`` 的短快取原始輸出，供 codex pid／tty 共用。

    含 tty 欄，讓 ``_codex_tty_map`` 能從同一份快照查表，不必再對每個 pid 各開一次
    ``ps -o tty= -p PID``。回傳 ``None`` 代表這次 ``ps`` 呼叫失敗（逾時／例外／非零
    exit）——是「不知道」，不是「沒有任何 process」；失敗不快取，下一輪重試。
    """
    global _ps_codex_snapshot_cache
    now = time.monotonic()
    if 0.0 <= now - _ps_codex_snapshot_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _ps_codex_snapshot_cache[1]
    try:
        result = subprocess.run(["ps", "-Ao", "pid=,tty=,comm=,args="], capture_output=True, text=True, timeout=3)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        # 非零 exit 跟逾時／例外一樣是「這次沒問到」，不是「問到了、答案是沒有 process」。
        return None
    _ps_codex_snapshot_cache = (now, result.stdout)
    return result.stdout


def running_codex_pids() -> list[int] | None:
    """目前活著的 codex CLI pid。回傳 ``None`` 代表這輪 ``ps`` 掃描失敗（未知），
    不是「沒有任何 codex process」；失敗不快取，下一輪重試。
    """
    global _codex_pids_cache
    now = time.monotonic()
    if 0.0 <= now - _codex_pids_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _codex_pids_cache[1]
    snapshot = _ps_codex_snapshot()
    if snapshot is None:
        return None
    pids: list[int] = []
    for pid, _tty, args in _parse_ps_codex_lines(snapshot):
        if _is_codex_internal_process(args):
            continue
        pids.append(pid)
    _codex_pids_cache = (now, pids)
    return pids


def _codex_tty_map() -> dict[int, str] | None:
    """pid → tty，來自 ``_ps_codex_snapshot()`` 共用的快照，不再多開一次 ``ps``。

    回傳 ``None`` 代表這輪掃描失敗（未知）；成功但某 pid 沒出現在快照裡（已死）
    時，該 pid 直接不在回傳 dict 內——呼叫端用 ``.get(pid, "")`` 取值即可。
    """
    snapshot = _ps_codex_snapshot()
    if snapshot is None:
        return None
    return {pid: tty for pid, tty, _args in _parse_ps_codex_lines(snapshot)}


def running_agent_pids() -> list[int]:
    """所有內建來源看得到的 live agent CLI 行程（顯示用途的彙總計數）。

    這裡刻意攤平 ``None``（掃描失敗／未知）成空清單——本函式只餵給 header 計數等
    顯示用途，不是存活判定；真正的 ENDED 判定路徑（``_hook_sessions``）用的是
    未攤平的 ``running_claude_pids`` / ``running_codex_pids`` 原始回傳值。
    """
    # 延後 import，避免 registry（Session model）與 sources package 初始化時循環相依。
    from ring.sources.local_llm import running_pids as running_local_llm_pids

    return [*(running_claude_pids() or []), *(running_codex_pids() or []), *running_local_llm_pids()]


def _pids_cwd(pids: list[int]) -> dict[int, str] | None:
    """批次查多個 pid 的 cwd：一次 ``lsof -a -p pid1,pid2,... -d cwd -Fn``（N 次 → 1 次）。

    PoC 已驗證（本機 lsof 4.91／macOS）：``-p`` 接受逗號分隔的多 pid；輸出以
    ``p<pid>`` 分段、其後 ``n<path>`` 行給該 pid 的 cwd。**其中一個 pid 已死時，
    lsof 對整批呼叫仍回傳 exit code 1**（連全部都死也是 1），但存活 pid 的區段照樣
    完整輸出——所以本函式刻意不看 ``returncode``，只要 subprocess 本身沒丟例外就
    解析 stdout。

    回傳 ``None`` 代表這次 lsof **呼叫本身**失敗（逾時／例外）——是「這輪不知道任何
    pid 的 cwd」（未知），呼叫端不得把它當「都沒有 cwd」用來判定 session 離場。
    批次呼叫成功但某個 pid 沒出現在輸出裡＝那個 pid 剛死／查無 cwd，是真資訊，不是
    未知，直接不進回傳的 dict（呼叫端用 ``.get(pid, "")`` 取值，缺項自然視為無 cwd）。
    """
    if not pids:
        return {}
    try:
        out = subprocess.run(
            ["lsof", "-a", "-p", ",".join(str(pid) for pid in pids), "-d", "cwd", "-Fn"],
            capture_output=True,
            text=True,
            timeout=3,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    result: dict[int, str] = {}
    current_pid: int | None = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                current_pid = int(line[1:])
            except ValueError:
                current_pid = None
        elif line.startswith("n") and current_pid is not None:
            result.setdefault(current_pid, line[1:])
    return result


def _pid_tty(pid: int) -> str:
    """claude process 的控制終端，正規化成 iTerm2 認得的 "/dev/ttysNNN"。

    單 pid、按需查詢用（例如 hook 事件當下要跳轉終端）；``discover_sessions()``
    每輪刷新的熱路徑改走 ``_claude_tty_map`` / ``_codex_tty_map``，不逐 pid 開 ``ps``。
    """
    try:
        tty = subprocess.run(["ps", "-o", "tty=", "-p", str(pid)], capture_output=True, text=True, timeout=3).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return _normalize_tty(tty)


def _claude_procs() -> list[tuple[str, str]] | None:
    """每個可聚焦的前景 Claude：(cwd, tty)。cwd 判活躍/分流，tty 給終端跳轉。

    背景 agent 不在這裡按 cwd 配對；它們由 source 依 process args 的 session id 精準
    認領，避免背景 process 的啟動 cwd 讓同專案舊 transcript 誤判成仍在場。
    同一個 cwd 可能同時開好幾個 session，所以之後在 cwd 群組裡只有 mtime 最新的
    這幾個算活著，其餘同專案的舊 session＝已離場。

    cwd／tty 各只批次查一次（``_pids_cwd`` 一次 lsof、``_claude_tty_map`` 沿用共用
    ps 快照），不再逐 pid 各開一次 lsof + 一次 ps。

    回傳 ``None`` 代表這輪掃描失敗（未知）——``ps``（pid 清單／tty）或 lsof（cwd）
    任一整批失敗都算，呼叫端（尤其是 ``_hook_sessions`` 的存活判定）不得把它當
    「沒有任何 claude process」處理。批次呼叫成功但個別 pid 沒查到 cwd/tty（剛死）
    不算未知，那個 pid 直接不貢獻一列，語意與逐 pid 版本一致。
    """
    global _claude_procs_cache
    now = time.monotonic()
    if 0.0 <= now - _claude_procs_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _claude_procs_cache[1]
    pids = running_foreground_claude_pids()
    if pids is None:
        return None
    cwd_by_pid = _pids_cwd(pids)
    if cwd_by_pid is None:
        return None
    tty_by_pid = _claude_tty_map()
    if tty_by_pid is None:
        return None
    procs: list[tuple[str, str]] = []
    for pid in pids:
        cwd = cwd_by_pid.get(pid, "")
        if cwd:
            procs.append((cwd, tty_by_pid.get(pid, "")))
    _claude_procs_cache = (now, procs)
    return procs


def _codex_procs() -> list[tuple[str, str]] | None:
    """每個還活著的 Codex CLI：(cwd, tty)。回傳 ``None`` 代表這輪掃描失敗（未知）。

    cwd／tty 各只批次查一次，理由與 ``_claude_procs`` 相同。
    """
    global _codex_procs_cache
    now = time.monotonic()
    if 0.0 <= now - _codex_procs_cache[0] <= _SUBPROCESS_CACHE_TTL:
        return _codex_procs_cache[1]
    pids = running_codex_pids()
    if pids is None:
        return None
    cwd_by_pid = _pids_cwd(pids)
    if cwd_by_pid is None:
        return None
    tty_by_pid = _codex_tty_map()
    if tty_by_pid is None:
        return None
    procs: list[tuple[str, str]] = []
    for pid in pids:
        cwd = cwd_by_pid.get(pid, "")
        if cwd:
            procs.append((cwd, tty_by_pid.get(pid, "")))
    _codex_procs_cache = (now, procs)
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
        with suppress(UnboundLocalError):
            con.close()

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
    for cwd, _tty in procs:
        if not cwd:  # _pids_cwd 查無此 pid cwd 的情況，沒 cwd 撐不起一列
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
    procs_by_provider: dict[str, list[tuple[str, str]] | None] | None = None,
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
            row = Session(
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
            if _promote_codex_permission_wait(
                _canonical_provider(provider),
                row.status,
                str(data.get("last_event", "")),
                time.time() - row.last_active,
                CODEX_PERMISSION_WAIT_SECONDS,
            ):
                row.status = Status.WAITING
                row.waiting_kind = "permission"
                pending_detail = data.get("pending_permission_detail")
                if isinstance(pending_detail, str) and pending_detail:
                    # hook 在裸 PermissionRequest 當下暫存的指令摘要（120s TTL 只管
                    # 「下一個事件來時還新不新鮮」；這裡 hook 靜默 = 同一筆請求還掛著，
                    # 摘要必然還是它，直接沿用）。
                    row.waiting_detail = pending_detail
            if _is_bare_session_start_row(
                row.tty or "",
                str(data.get("last_event", "")),
                time.time() - row.last_active,
            ):
                # 背景 job／agent 的承載 session：只報到、不做事、沒有終端。判離場後
                # 預設收起（`--all` / TUI 的 a 仍查得到），它真的動起來時會自己回來。
                row.status = Status.ENDED
            out.append(row)
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
    #   4. 「這個 provider 這輪 ps／lsof 掃描失敗」（值為 ``None``）是「未知」，不是
    #      「真的偵測到零個 live process」——未知時完全不動這個 provider 底下任何一筆
    #      row 的狀態，保留既有狀態，避免單次系統瞬間卡頓（ps timeout）把整版 session
    #      誤判 ENDED（見 ring-vanishing-sessions 診斷）。
    if out:
        proc_counts: dict[tuple[str, str], int] = {}
        proc_ttys: dict[tuple[str, str], set[str]] = {}
        proc_cwds_by_provider: dict[str, list[str]] = {}
        unknown_providers: set[str] = set()
        for pk in _PROVIDER_PROCS:
            if procs_by_provider is not None:
                provider_procs = procs_by_provider.get(pk, [])
            else:
                # 沒有帶 procs_by_provider 時，直接沿用 procs 這個 sentinel-None（單純
                # 代表「呼叫端沒給」，不是「這輪掃描失敗」），維持既有「當空清單」語意。
                provider_procs = procs if procs is not None else []
            if provider_procs is None:
                # 這輪掃描失敗（ps/lsof timeout 或例外）——標成未知，底下的存活判定
                # 對這個 provider 一律跳過，不把任何 row 判 ENDED。
                unknown_providers.add(pk)
                continue
            for cwd, tty in provider_procs:
                real_cwd = _real(cwd)
                key = (pk, real_cwd)
                proc_counts[key] = proc_counts.get(key, 0) + 1
                if tty:
                    proc_ttys.setdefault(key, set()).add(tty)
                proc_cwds_by_provider.setdefault(pk, []).append(real_cwd)

        # 背景 agent 的 process 沒有可聚焦終端，不能拿來替同 cwd 的舊前景 hook row
        # 證明存活；它只精準認領 args 裡明載的 session id。Claude scan 仍使用包含背景
        # agent 的 session-id 配對，因此沒有 hook row 的 agent 也不會從看板消失。
        # 這次呼叫失敗（``None``＝未知）時保守當成「沒有已知背景 agent」——claude-code
        # 這個 provider 本身這輪多半也偵測失敗，會被上面的 unknown_providers 整批保護，
        # 這裡的 fallback 只是避免拿 None 做 in 運算炸掉。
        background_ids = background_agent_session_ids() or set()
        rows_by_key: dict[tuple[str, str], list[Session]] = {}
        for s in out:
            pk = _canonical_provider(s.provider)
            if pk not in _PROVIDER_PROCS:
                continue  # 沒有 proc 偵測器 → 無法驗活性 → fail-open，交給 SessionEnd
            if pk == "claude-code" and s.session_id in background_ids:
                continue
            rows_by_key.setdefault((pk, _real(s.cwd)), []).append(s)

        for key, rows in rows_by_key.items():
            pk, row_cwd = key
            if pk in unknown_providers:
                continue  # 這輪掃描失敗，未知不等於離場，保留既有狀態
            live_n = proc_counts.get(key, 0)
            if live_n == 0:
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
