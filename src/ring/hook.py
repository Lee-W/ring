"""RiNG 的 provider-neutral hook handler——精準狀態的來源。

Agent CLI 在各事件把一段 JSON 從 stdin 餵進來。我們據此 upsert 一份
``~/.config/ring/sessions/<session_id>.json``，讓 registry 讀到精準狀態
（zero-config 靠 mtime / state 猜不出「在等你」，這裡靠事件直接知道）。

設計原則：hook 永遠 exit 0、不擋住 session；解析失敗就安靜放行。

事件 → 狀態：
  SessionStart / UserPromptSubmit → 🟢 工作中（剛開始 / 你剛回話，台上在跑）
  PreToolUse（非 action）/ PostToolUse → 🟢 工作中（工具在動，順便清掉剛答完的等你）
  Stop                            → 🟡 跑完停著（回完一輪，不代表需要你回應）；但最後一則
                                    assistant 訊息「結尾」像純文字提問時 → 🔴 等你
                                    （waiting_kind="question"，見 question_detect.py；
                                    config 鍵 detect_stop_questions 可關閉，預設開）
  PermissionRequest（裸）          → 🟢 工作中（權限判定中，多半瞬間自動放行；Claude Code
                                    真的停下來等人由後續的 permission_prompt Notification
                                    兜底轉 🔴；Codex 沒有這種事件，改由讀取側的靜默逾時
                                    判定補上，見 registry._promote_codex_permission_wait）
  actionable Notification / AskUserQuestion → 🔴 等你（需要你決策）
  SessionEnd                      → 刪檔（乾淨離場）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ring.config import get_config
from ring.hook_protocol import HOOK_EVENTS, NormalizedHookEvent, adapter_for, provider_from_payload
from ring.i18n import gettext as _
from ring.i18n import set_lang
from ring.payload_log import maybe_log_raw_payload
from ring.question_detect import stop_last_assistant_text, trailing_question_detail
from ring.registry import (
    RING_REGISTRY,
    Session,
    Status,
    _pid_tty,
    unhide_session,
)
from ring.stats import log_transition
from ring.transcript import _extract_todo, _latest_action, _tail_records

_HOOK_EVENTS = list(HOOK_EVENTS)

# Codex 的 hooks.json 用跟 Claude 同樣的 PascalCase 事件名，但只支援其中一小撮。
# 保守取有實證可用的：PermissionRequest（裸事件只代表權限準備判定；明確需互動才 → 🔴）、
# PreToolUse（→ 動作/清除）、PostToolUse（工具跑完 → 清除；0.144.4 binary 內嵌 schema
# 證實存在——它是「核可後最早的下一個事件」，能盡早清掉讀取側的核可等待判定）、
# Stop（→ 🟡 回合結束、清掉 waiting）。多裝 Codex 不認的事件有風險，故不照搬 Claude 全套。
_CODEX_HOOK_EVENTS = ["PreToolUse", "PermissionRequest", "PostToolUse", "Stop"]

# hook command 的 timeout（秒）。給足，因為 notify_backend="agent-hooks" 時權限 modal 會
# block 到使用者作答。install 用它判斷「既有條目要不要更新」——舊版裝的 timeout=10 會被升上來。
_HOOK_TIMEOUT = 600


@dataclass(frozen=True)
class _HookTarget:
    """一個 hook 安裝目標：settings 檔、要裝的事件、command、重啟提示。"""

    path: Path
    events: list[str]
    command: str
    restart_hint: str


def _hook_targets() -> list[_HookTarget]:
    """目前適用的安裝目標。Claude 一律裝；Codex 只在 ``~/.codex`` 存在（有在用）時才裝。"""
    home = Path.home()
    targets = [
        _HookTarget(
            home / ".claude" / "settings.json",
            _HOOK_EVENTS,
            "ring hook",
            _("重開 Claude Code session 後，🔴 等你狀態就會精準起來。"),
        )
    ]
    if (home / ".codex").is_dir():
        targets.append(
            _HookTarget(
                home / ".codex" / "hooks.json",
                _CODEX_HOOK_EVENTS,
                "ring hook --provider codex",
                _(
                    "重開 Codex session、並在它詢問時「信任」這個 hook，🔴 等你狀態才會精準起來"
                    "（Codex 不會執行未信任的 hook）。"
                ),
            )
        )
    return targets


def _ps_row(pid: int) -> tuple[int, str] | None:
    """回傳給定 pid 的 (ppid, comm)；失敗回 None。"""
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=2
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    parts = out.split(None, 1)
    if len(parts) < 2:
        return None
    try:
        return int(parts[0]), parts[1]
    except ValueError:
        return None


def _session_tty(process_names: tuple[str, ...]) -> str:
    """hook 是 agent CLI 的後代——往上找到 provider process，回它的控制終端 tty。

    這是「session → 哪個終端」最精準的對應（不必靠 cwd 猜），給 iTerm2 跳轉用。
    """
    if not process_names:
        return ""
    pid = os.getppid()
    for _attempt in range(12):
        row = _ps_row(pid)
        if row is None:
            return ""
        ppid, comm = row
        if os.path.basename(comm.strip()) in process_names:
            return _pid_tty(pid)
        if ppid <= 1:
            return ""
        pid = ppid
    return ""


def _controlling_tty() -> str:
    """hook process 自己的 controlling tty。非互動環境取不到時回空字串。"""
    try:
        return os.ttyname(sys.stdin.fileno())
    except OSError:
        return ""


def run_hook(provider: str = "claude-code") -> int:
    """讀一次 stdin → 寫 RiNG registry 狀態（看板）→ 轉等你時就地發通知 →（可選）委派 agent-hooks。

    轉 🔴 等你的事件，會在當下直接發系統通知（見 ``_ring_waiting_now``）——不必開著看板，
    關掉終端也照樣 ring 你。委派只在 ``notify_backend == "agent-hooks"`` 且 PATH 上有
    ``agent-hooks`` 時發生：把原始 payload 透傳給 ``agent-hooks callback``，由它同步出 modal、
    收按鈕、把決策寫到 stdout 回給 Claude（這條路下 ``_ring_waiting_now`` 自動短路、不重複發）。
    其餘情況 RiNG 記狀態 + 發通知後 exit 0（你在終端自己回答）。

    每次執行開頭先嘗試懶惰 flush 合流 queue（見 ``notify_queue.flush_if_due``）——沒有常駐
    daemon，任何 hook 事件（不限於這次自己是不是 waiting 事件）都是「debounce 視窗過期」的
    天然觸發點，讓 headless（沒開 TUI）也能補發彙總通知。失敗安靜吞掉，不影響本次事件記錄。
    """
    try:
        from ring.notify_queue import flush_if_due

        flush_if_due()
    except Exception:
        pass

    try:
        raw = sys.stdin.read()
    except OSError:
        return 0
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(data, dict):
        return 0

    selected_provider = provider_from_payload(data, fallback=provider)
    # 取證 log：診斷用、預設關閉（見 payload_log.py）。故意放在任何狀態判定/改寫之前，
    # 記下 hook 實際收到的原始資料——不影響、不參與下面的正規化流程。
    maybe_log_raw_payload(selected_provider, data)
    _record_session_state(data, selected_provider)
    return _delegate_to_agent_hooks(raw, selected_provider)


def _record_session_state(data: dict[str, Any], selected_provider: str) -> None:
    """把事件正規化後 upsert / 刪除 RiNG registry 檔（看板狀態）。失敗安靜吞，不回傳。"""
    adapter = adapter_for(selected_provider)
    event = adapter.normalize(data)
    if event is None:
        return
    event = _maybe_flag_trailing_question(event, data)

    unhide_session(event.session_id)
    path = RING_REGISTRY / f"{quote(event.session_id, safe=':')}.json"
    prev_row = _previous_row(path)
    prev_status = str(prev_row["status"]) if "status" in prev_row else None
    if event.status is Status.ENDED:
        path.unlink(missing_ok=True)  # 乾淨離場：直接消失
        if prev_status is not None and prev_status != Status.ENDED.value:
            log_transition(event.session_id, event.provider, event.cwd, Status.ENDED.value)
        return

    last_action, todo = event.last_action or "—", None
    tp = event.transcript_path
    if tp:
        records = _tail_records(Path(tp))
        if records:
            last_action = _latest_action(records)
            todo = _extract_todo(records)

    now = time.time()
    payload: dict[str, Any] = {
        "session_id": event.session_id,
        "provider": event.provider,
        "cwd": event.cwd,
        "origin_cwd": event.cwd,
        "status": event.status.value,
        # 最後一個 hook 事件名：讀取側據此做 codex 的核可等待判定（最後事件是
        # PermissionRequest 且靜默逾時 → 🔴），任何後續事件覆寫它就自然清紅。
        "last_event": event.event,
        "last_active": now,
        "heartbeat_at": now,
        "last_action": last_action,
        "hook_pid": os.getpid(),
    }
    if tp:
        payload["source_path"] = tp
    tmux_pane = os.environ.get("TMUX_PANE", "").strip()
    if tmux_pane:
        payload["tmux_pane"] = tmux_pane
    if todo:
        payload["todo"] = list(todo)
    if event.waiting_for:
        payload["waiting_for"] = event.waiting_for

    # 裸 PermissionRequest（→ WORKING）帶著最具體的 tool/指令摘要；真的停下來等人時，
    # Claude Code 只補發籠統的 permission_prompt Notification（"Claude needs your
    # permission"，不含 tool/指令）。所以把摘要暫存進 row，Notification 轉 WAITING 時
    # （還新鮮的話）拿它當 waiting_detail，看板與通知才看得到具體是哪條指令在等核可。
    # Codex 沒有 Notification：這份暫存改由讀取側的靜默逾時判定拿去當 waiting_detail
    # （見 registry._promote_codex_permission_wait）。
    pending_detail, pending_at = _pending_permission_detail(event, prev_row, now)
    if pending_detail:
        payload["pending_permission_detail"] = pending_detail
        payload["pending_permission_detail_at"] = pending_at

    if event.status is Status.WAITING and event.waiting_kind:
        payload["waiting_kind"] = event.waiting_kind
    if event.status is Status.WAITING:
        waiting_detail = event.detail
        if event.event == "Notification" and event.waiting_kind == "permission" and pending_detail:
            waiting_detail = pending_detail
        if waiting_detail:
            payload["waiting_detail"] = waiting_detail
    tty = event.tty or _controlling_tty() or _session_tty(adapter.process_names)
    if tty:
        payload["tty"] = tty

    # waiting 通知冷卻（防翻轉轟炸）：hook 是短命 process，冷卻狀態持久化在 registry row
    # 的 waiting_notified_at（上次 waiting 通知的 epoch 時間戳）。離開 WAITING 也要把時間戳
    # 帶進新 row（payload 每次重建，不帶會遺失），快速翻回 WAITING 時才判得出「還在冷卻期」。
    # 舊 row 沒這欄位 = 從未通知過 → 照發。並發寫沿用既有 tmp+replace 的 last-write-wins。
    last_notified = _as_epoch(prev_row.get("waiting_notified_at"))
    if last_notified is not None:
        payload["waiting_notified_at"] = last_notified
    should_ring = False
    if event.status is Status.WAITING:
        should_ring = _waiting_ring_allowed(last_notified, now)
        if should_ring:
            payload["waiting_notified_at"] = now

    RING_REGISTRY.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)  # atomic

    if prev_status != event.status.value:
        log_transition(event.session_id, event.provider, event.cwd, event.status.value)

    if should_ring:
        _ring_waiting_now(event, payload, last_action)


def _maybe_flag_trailing_question(event: NormalizedHookEvent, data: dict[str, Any]) -> NormalizedHookEvent:
    """Stop 事件、目前判定 🟡 跑完停著、且訊息結尾像純文字提問時，升級成 🔴 等你。

    這是 hook_protocol._ALWAYS_STATUS 的 Stop→IDLE 之外唯一會讀 assistant 文字的地方——
    寫死的映射本身測不到「agent 用文字問了問題但沒用 AskUserQuestion」這種情況（見
    ring.question_detect 模組 docstring）。保守判準：只看訊息結尾，中段有問句、結尾是
    陳述句一律不算，寧可漏報不要誤報。config 關閉（detect_stop_questions=false）或非
    Stop / 非 IDLE 一律原樣放行；抓不到文字或結尾不是問句也放行。
    """
    if event.event != "Stop" or event.status is not Status.IDLE:
        return event
    if not get_config().detect_stop_questions:
        return event
    text = stop_last_assistant_text(data)
    detail = trailing_question_detail(text)
    if not detail:
        return event
    return replace(event, status=Status.WAITING, waiting_kind="question", detail=detail)


def _previous_row(path: Path) -> dict[str, Any]:
    """讀 registry 檔目前的內容（給轉換偵測與通知冷卻用）；沒檔 / 壞檔回空 dict。"""
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _as_epoch(v: Any) -> float | None:
    """把 row 欄位值安全轉成 epoch float；缺欄位 / 型別不對回 None（視為從未通知）。"""
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else None


# 暫存的權限 detail 新鮮期（秒）：permission_prompt Notification 通常在裸 PermissionRequest
# 後 ~6 秒到；超過這個窗口的暫存視為過期（大概率屬於已解決的舊請求），不再拿來當 detail。
_PENDING_DETAIL_TTL = 120.0

# 這些事件代表權限已放行（工具跑完）或回合已推進（新輸入 / 回合結束）→ 清掉暫存。
_PENDING_DETAIL_CLEAR_EVENTS = {"PostToolUse", "UserPromptSubmit", "Stop"}


def _pending_permission_detail(event: NormalizedHookEvent, prev_row: dict[str, Any], now: float) -> tuple[str, float]:
    """決定這次要寫進 row 的「暫存權限 detail」：(detail, 暫存時間戳)；不留就回 ("", 0.0)。

    - 裸 PermissionRequest（→ WORKING）帶 detail → 以這次的 detail 起新暫存
    - PostToolUse / UserPromptSubmit / Stop → 清掉（已放行或回合推進，舊暫存失效）
    - 其餘事件 → 沿用 prev_row 的暫存（row 每次重建，不帶會遺失），但過期就丟
    """
    if event.event == "PermissionRequest" and event.status is Status.WORKING and event.detail:
        return event.detail, now
    if event.event in _PENDING_DETAIL_CLEAR_EVENTS:
        return "", 0.0
    prev_detail = prev_row.get("pending_permission_detail")
    prev_at = _as_epoch(prev_row.get("pending_permission_detail_at"))
    if isinstance(prev_detail, str) and prev_detail and prev_at is not None and now - prev_at <= _PENDING_DETAIL_TTL:
        return prev_detail, prev_at
    return "", 0.0


def _waiting_ring_allowed(last_notified: float | None, now: float) -> bool:
    """hook 的 waiting 通知是否放行：距上次通知未滿 ``waiting_cooldown_seconds`` 就不發。

    背景 subagent 的權限請求會讓 session 以 30 秒～2 分鐘頻率在 working↔waiting 間翻轉，
    每次轉入都發一則系統通知會轟炸使用者。cooldown=0 = 關閉（維持現行為：每次轉入都發）；
    從未通知過（``last_notified`` 為 None，含舊版 row 沒有該欄位）一律放行。
    """
    cooldown = get_config().waiting_cooldown_seconds
    if cooldown <= 0 or last_notified is None:
        return True
    return now - last_notified >= cooldown


def _ring_waiting_now(event: Any, payload: dict[str, Any], last_action: str) -> None:
    """session 轉 🔴 等你的當下，就地由 hook 發系統通知——不必等 RiNG 看板輪詢。

    WAITING 是 hook 資料驅動的（scan 模式永不標 WAITING），hook 是第一手知道「等你」
    的地方；在事件當下通知最即時，也不依賴看板有沒有開著（看板沒開時舊版根本不會 ring
    你）。唯一的例外是 codex 的核可等待：那是讀取側對 hook row 的靜默逾時判定，沒有
    對應的 hook 事件可在這裡發通知，由 TUI 的提醒排程器代發（見 tui._ring_on_waiting_alerts）。
    backend=none / agent-hooks 由 notify_waiting → _select_notifier 自動短路
    （agent-hooks 改走 _delegate_to_agent_hooks 的 modal 委派），不會重複發。
    失敗安靜吞掉，絕不擋住 session。
    """
    try:
        from ring.notify import notify_waiting

        notify_waiting(
            [
                Session(
                    session_id=event.session_id,
                    cwd=event.cwd,
                    status=Status.WAITING,
                    last_active=float(payload.get("last_active", time.time())),
                    last_action=last_action,
                    source="hook",
                    tty=payload.get("tty"),
                    provider=event.provider,
                    waiting_kind=str(payload.get("waiting_kind", "")),
                    waiting_detail=str(payload.get("waiting_detail", "")),
                    origin_cwd=event.cwd,
                )
            ]
        )
    except Exception:
        pass


def _delegate_to_agent_hooks(raw: str, selected_provider: str) -> int:
    """notify_backend=="agent-hooks" 且 agent-hooks 在 PATH → 透傳 payload 給它出決策。

    把原始 stdin 餵給 ``agent-hooks callback``，stdout 直接繼承（agent-hooks 把 hook
    response 寫給 Claude）。回傳 agent-hooks 的 exit code。沒設 / 沒裝 / 失敗 → 回 0，
    不影響 session。不設內部 timeout：權限對話框會 block 到使用者作答（外層由 Claude
    的 hook timeout 控）。
    """
    if get_config().notify_backend != "agent-hooks":
        return 0
    if shutil.which("agent-hooks") is None:
        return 0
    cmd = ["agent-hooks", "callback"]
    if selected_provider in {"claude-code", "codex"}:
        cmd += ["--provider", selected_provider]
    try:
        return subprocess.run(cmd, input=raw, text=True).returncode
    except Exception:
        return 0


@dataclass(frozen=True)
class HookStatus:
    """某個 hook provider 的安裝狀態快照（唯讀，不拋例外）。"""

    provider: str  # "claude-code" | "codex"
    path: Path  # target settings 檔
    applicable: bool  # 這個 target 目前適用嗎（Codex：~/.codex 是否存在）
    installed: bool  # 檔內是否已有 RiNG hook 條目
    exists: bool  # 設定檔本身是否存在


def _has_ring_entry(groups: list[Any]) -> bool:
    """掃 hook groups，回傳是否有任何 _is_ring_hook_command 命中的條目。"""
    for g in groups:
        if not isinstance(g, dict):
            continue
        for h in g.get("hooks", []):
            if _is_ring_hook_command(h.get("command", "")):
                return True
    return False


def hook_status() -> list[HookStatus]:
    """逐一檢視 claude-code / codex 兩個 provider，回報每個的安裝狀態（唯讀，不寫檔）。

    無條件回報兩個 provider（claude-code 永遠 applicable=True，codex 視 ~/.codex 而定）；
    讀檔失敗（檔不存在 / 非合法 JSON）一律當 installed=False，exists 照實填，不拋例外。
    """
    home = Path.home()
    providers = [
        {
            "provider": "claude-code",
            "path": home / ".claude" / "settings.json",
            "applicable": True,
            "events": _HOOK_EVENTS,
        },
        {
            "provider": "codex",
            "path": home / ".codex" / "hooks.json",
            "applicable": (home / ".codex").is_dir(),
            "events": _CODEX_HOOK_EVENTS,
        },
    ]
    result: list[HookStatus] = []
    for p in providers:
        path = p["path"]
        assert isinstance(path, Path)
        exists = path.exists()
        installed = False
        if exists:
            try:
                data: dict[str, Any] = json.loads(path.read_text() or "{}")
                hooks = data.get("hooks", {})
                events = p["events"]
                assert isinstance(events, list)
                installed = any(_has_ring_entry(list(hooks.get(event) or [])) for event in events)
            except Exception:
                installed = False
        result.append(
            HookStatus(
                provider=str(p["provider"]),
                path=path,
                applicable=bool(p["applicable"]),
                installed=installed,
                exists=exists,
            )
        )
    return result


def _is_ring_hook_command(cmd: str) -> bool:
    """判斷一條 command 字串是否為 RiNG 自己安裝的 hook 條目。

    判定規則（涵蓋新舊兩種形式，也涵蓋帶 provider 參數的形式）：
    - 把 command 以空白切成 tokens；
    - 前兩個 tokens 是 ``ring hook``（第一個可為 full path）。

    涵蓋：``"ring hook"``、``"/usr/local/bin/ring hook"``、``"ring hook --provider codex"``。
    不命中：``"some-other-tool hook"``、``"ring"``、``""``、別人裝的任意 command。
    """
    tokens = cmd.split()
    if len(tokens) < 2:
        return False
    return os.path.basename(tokens[0]) == "ring" and tokens[1] == "hook"


# install-hooks 會就「使用者互動」事件警告：別的工具若也掛在這上面（彈自己的對話框 /
# 通知），會跟 RiNG 的通知重複觸發。其餘事件（PostToolUse 掛 formatter 之類）是正常
# 共存，不警告。
_CONFLICT_EVENTS = ("PermissionRequest", "Notification")


def _coresident_handlers(hooks: dict[str, Any]) -> list[str]:
    """列出掛在「使用者互動」事件上、非 RiNG 的 command（去重、保序）。

    這些 command 會跟 RiNG 在同一批事件觸發；若想完全改用 RiNG，通常得移除它們，
    免得通知 / dialog 重複。回傳空清單代表沒有衝突。
    """
    seen: list[str] = []
    for event in _CONFLICT_EVENTS:
        for g in hooks.get(event) or []:
            if not isinstance(g, dict):
                continue
            for h in g.get("hooks", []):
                cmd = h.get("command", "")
                if cmd and not _is_ring_hook_command(cmd) and cmd not in seen:
                    seen.append(cmd)
    return seen


def _print_conflict_warning(hooks: dict[str, Any]) -> None:
    """偵測到其他工具也掛在使用者互動事件上時，提醒它們會跟 RiNG 重複觸發。"""
    conflicts = _coresident_handlers(hooks)
    if not conflicts:
        return
    print(_("⚠️ 偵測到其他工具也掛在 {events}：{cmds}", events=", ".join(_CONFLICT_EVENTS), cmds=", ".join(conflicts)))
    print("   " + _("它們會跟 RiNG 的通知重複觸發；要完全改用 RiNG，建議移除它們。"))


def _remove_ring_hooks_from_groups(groups: list[Any]) -> tuple[list[Any], bool]:
    """從 hook groups 列表中移除所有 _is_ring_hook_command 命中的條目，並清掉變空的 group。

    回傳 (cleaned_groups, was_changed)：
    - cleaned_groups：移除後的 groups（因此變空的 group 也被移除）；
    - was_changed：是否有任何條目被移除。
    """
    new_groups = []
    changed = False
    for g in groups:
        if not isinstance(g, dict):
            new_groups.append(g)
            continue
        hooks_in_g = g.get("hooks", [])
        filtered = [h for h in hooks_in_g if not _is_ring_hook_command(h.get("command", ""))]
        if len(filtered) < len(hooks_in_g):
            changed = True
            # 有條目被移除：若還有其他 hook 保留就縮減；group 清空就整個丟掉
            if filtered:
                new_groups.append({**g, "hooks": filtered})
            # else: group 清空，不加回
        else:
            new_groups.append(g)
    return new_groups, changed


def install_hooks(dry_run: bool = False) -> int:
    """把 RiNG 的 hook 註冊進 Claude（~/.claude/settings.json）與 Codex（~/.codex/hooks.json，
    僅在 ~/.codex 存在時）。合併，不覆蓋既有 hooks。任一目標失敗回非 0。"""
    set_lang(get_config().lang)
    rc = 0
    for target in _hook_targets():
        rc |= _install_target(target, dry_run)
    return rc


def _install_target(target: _HookTarget, dry_run: bool) -> int:
    """把 RiNG hook 合併進單一 target 的 settings 檔。"""
    settings = target.path
    cmd = target.command
    data: dict[str, Any] = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text() or "{}")
        except json.JSONDecodeError:
            print(_("⚠️ {path} 不是合法 JSON，先處理它再來。", path=settings))
            return 1

    hooks = data.setdefault("hooks", {})

    # 第一輪：掃描各 event，判斷是否需要變更（已有完全正確 cmd 且無舊 full-path 形式 → 不動）。
    events_need_change: list[str] = []
    for event in target.events:
        groups = list(hooks.get(event) or [])
        # 「已正確」＝有條目 cmd 完全相符且 timeout 已是現值。timeout 不同（例如舊版裝的 10）
        # 也算需要更新，這樣 install-hooks 能自我修復過時的 timeout。
        already_exact = any(
            h.get("command") == cmd and h.get("timeout") == _HOOK_TIMEOUT
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
        )
        has_old_path = any(
            _is_ring_hook_command(h.get("command", "")) and h.get("command") != cmd
            for g in groups
            if isinstance(g, dict)
            for h in g.get("hooks", [])
        )
        if not already_exact or has_old_path:
            events_need_change.append(event)

    if dry_run:
        for event in target.events:
            groups = list(hooks.get(event) or [])
            cleaned, _changed = _remove_ring_hooks_from_groups(groups)
            # timeout 給足：notify_backend="agent-hooks" 時權限 modal 會 block 到使用者作答。
            cleaned.append({"hooks": [{"type": "command", "command": cmd, "timeout": _HOOK_TIMEOUT}]})
            hooks[event] = cleaned
        print(f"# dry-run → {settings}\n")
        print(json.dumps({"hooks": {e: hooks[e] for e in target.events}}, indent=2, ensure_ascii=False))
        return 0

    if not events_need_change:
        print(_("✅ RiNG hooks 已經裝過了，沒有變更。（{path}）", path=settings))
        _print_conflict_warning(hooks)
        return 0

    for event in events_need_change:
        groups = list(hooks.get(event) or [])
        cleaned, _changed = _remove_ring_hooks_from_groups(groups)
        # timeout 給足：notify_backend="agent-hooks" 時權限 modal 會 block 到使用者作答。
        cleaned.append({"hooks": [{"type": "command", "command": cmd, "timeout": _HOOK_TIMEOUT}]})
        hooks[event] = cleaned

    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(_("✅ 已註冊 RiNG hooks（{events}）到 {path}", events=", ".join(events_need_change), path=settings))
    print("   " + target.restart_hint)
    _print_conflict_warning(hooks)
    return 0


def uninstall_hooks(dry_run: bool = False) -> int:
    """從 Claude 與 Codex（若有）的 settings 移除所有 RiNG hook 條目（對稱於 install_hooks）。"""
    set_lang(get_config().lang)
    rc = 0
    for target in _hook_targets():
        rc |= _uninstall_target(target, dry_run)
    return rc


def _uninstall_target(target: _HookTarget, dry_run: bool) -> int:
    """從單一 target 的 settings 檔移除 RiNG hook 條目。"""
    settings = target.path
    if not settings.exists():
        print(_("ℹ️ {path} 不存在，沒有可移除的 hook。", path=settings))
        return 0

    try:
        data: dict[str, Any] = json.loads(settings.read_text() or "{}")
    except json.JSONDecodeError:
        print(_("⚠️ {path} 不是合法 JSON，先處理它再來。", path=settings))
        return 1

    hooks = data.get("hooks", {})
    removed: list[str] = []
    for event in target.events:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        cleaned, was_removed = _remove_ring_hooks_from_groups(groups)
        if was_removed:
            removed.append(event)
            hooks[event] = cleaned

    if dry_run:
        print(f"# dry-run → {settings}\n")
        preview_hooks = {e: hooks.get(e, []) for e in target.events}
        print(json.dumps({"hooks": preview_hooks}, indent=2, ensure_ascii=False))
        return 0

    if not removed:
        print(_("ℹ️ 沒有找到 RiNG hook 條目，無需移除。（{path}）", path=settings))
        return 0

    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(_("✅ 已移除 RiNG hooks（{events}）從 {path}", events=", ".join(removed), path=settings))
    return 0
