"""RiNG 的 provider-neutral hook handler——精準狀態的來源。

Agent CLI 在各事件把一段 JSON 從 stdin 餵進來。我們據此 upsert 一份
``~/.config/ring/sessions/<session_id>.json``，讓 registry 讀到精準狀態
（zero-config 靠 mtime / state 猜不出「在等你」，這裡靠事件直接知道）。

設計原則：hook 永遠 exit 0、不擋住 session；解析失敗就安靜放行。

事件 → 狀態：
  SessionStart / UserPromptSubmit → 🟢 WORKING（剛開始 / 你剛回話，台上在跑）
  PreToolUse（非 action）/ PostToolUse → 🟢 WORKING（工具在動，順便清掉剛答完的 WAITING）
  Stop                            → 🟡 IDLE（回完一輪，不代表需要你回應）
  PermissionRequest / actionable Notification / AskUserQuestion → 🔴 WAITING（需要你決策）
  SessionEnd                      → 刪檔（乾淨離場）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from ring.config import get_config
from ring.hook_protocol import HOOK_EVENTS, adapter_for, provider_from_payload
from ring.i18n import gettext as _
from ring.i18n import set_lang
from ring.registry import (
    RING_REGISTRY,
    Status,
    _extract_todo,
    _latest_action,
    _pid_tty,
    _tail_records,
)

_HOOK_EVENTS = list(HOOK_EVENTS)

# Codex 的 hooks.json 用跟 Claude 同樣的 PascalCase 事件名，但只支援其中一小撮。
# 保守取有實證可用的：PermissionRequest（→ 🔴 等核可）、PreToolUse（→ 動作/清除）、
# Stop（→ 🟡 回合結束、清掉 waiting）。多裝 Codex 不認的事件有風險，故不照搬 Claude 全套。
_CODEX_HOOK_EVENTS = ["PreToolUse", "PermissionRequest", "Stop"]


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
            _("重開 Claude Code session 後，🔴 待回覆狀態就會精準起來。"),
        )
    ]
    if (home / ".codex").is_dir():
        targets.append(
            _HookTarget(
                home / ".codex" / "hooks.json",
                _CODEX_HOOK_EVENTS,
                "ring hook --provider codex",
                _("重開 Codex session 後，🔴 待回覆狀態就會精準起來。"),
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


def run_hook(provider: str = "claude-code") -> int:
    """讀一次 stdin → 寫 RiNG registry 狀態（看板）→（可選）把決策委派給 agent-hooks。

    委派只在 ``notify_backend == "agent-hooks"`` 且 PATH 上有 ``agent-hooks`` 時發生：
    把原始 payload 透傳給 ``agent-hooks callback``，由它同步出 modal、收按鈕、把決策
    寫到 stdout 回給 Claude。其餘情況 RiNG 只記狀態、exit 0（你在終端自己回答）。
    """
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
    _record_session_state(data, selected_provider)
    return _delegate_to_agent_hooks(raw, selected_provider)


def _record_session_state(data: dict[str, Any], selected_provider: str) -> None:
    """把事件正規化後 upsert / 刪除 RiNG registry 檔（看板狀態）。失敗安靜吞，不回傳。"""
    adapter = adapter_for(selected_provider)
    event = adapter.normalize(data)
    if event is None:
        return

    path = RING_REGISTRY / f"{quote(event.session_id, safe=':')}.json"
    if event.status is Status.ENDED:
        path.unlink(missing_ok=True)  # 乾淨離場：直接消失
        return

    last_action, todo = event.last_action or "—", None
    tp = event.transcript_path
    if tp:
        records = _tail_records(Path(tp))
        if records:
            last_action = _latest_action(records)
            todo = _extract_todo(records)

    payload: dict[str, Any] = {
        "session_id": event.session_id,
        "provider": event.provider,
        "cwd": event.cwd,
        "origin_cwd": event.cwd,
        "status": event.status.value,
        "last_active": time.time(),
        "last_action": last_action,
    }
    if todo:
        payload["todo"] = list(todo)
    if event.waiting_for:
        payload["waiting_for"] = event.waiting_for
    tty = event.tty or _session_tty(adapter.process_names)
    if tty:
        payload["tty"] = tty

    RING_REGISTRY.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)  # atomic


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
        already_exact = any(h.get("command") == cmd for g in groups if isinstance(g, dict) for h in g.get("hooks", []))
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
            cleaned.append({"hooks": [{"type": "command", "command": cmd, "timeout": 600}]})
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
        cleaned.append({"hooks": [{"type": "command", "command": cmd, "timeout": 600}]})
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
