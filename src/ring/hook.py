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
import subprocess
import sys
import time
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
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0

    selected_provider = provider_from_payload(data, fallback=provider)
    adapter = adapter_for(selected_provider)
    event = adapter.normalize(data)
    if event is None:
        return 0

    path = RING_REGISTRY / f"{quote(event.session_id, safe=':')}.json"
    if event.status is Status.ENDED:
        path.unlink(missing_ok=True)  # 乾淨離場：直接消失
        return 0

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


def _ring_command() -> str:
    return "ring hook"


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
    """把 RiNG 的 hook 註冊進 ~/.claude/settings.json（合併，不覆蓋既有 hooks）。"""
    set_lang(get_config().lang)
    settings = Path.home() / ".claude" / "settings.json"
    data: dict[str, Any] = {}
    if settings.exists():
        try:
            data = json.loads(settings.read_text() or "{}")
        except json.JSONDecodeError:
            print(_("⚠️ {path} 不是合法 JSON，先處理它再來。", path=settings))
            return 1

    cmd = _ring_command()
    hooks = data.setdefault("hooks", {})

    # 第一輪：掃描各 event，判斷是否有任何 event 需要變更
    # - already_exact：該 event 下已有完全正確的 "ring hook"（無需動）
    # - has_old：該 event 下有舊 full-path 形式的 ring hook（需替換）
    # - has_none：該 event 下沒有任何 ring hook（需新增）
    events_need_change: list[str] = []
    for event in _HOOK_EVENTS:
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
        # dry_run：先套用變更到 hooks（不寫檔）再顯示
        for event in _HOOK_EVENTS:
            groups = list(hooks.get(event) or [])
            cleaned, _changed = _remove_ring_hooks_from_groups(groups)
            cleaned.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
            hooks[event] = cleaned
        print(f"# dry-run → {settings}\n")
        print(json.dumps({"hooks": {e: hooks[e] for e in _HOOK_EVENTS}}, indent=2, ensure_ascii=False))
        return 0

    if not events_need_change:
        print(_("✅ RiNG hooks 已經裝過了，沒有變更。"))
        return 0

    # 第二輪：只對需要變更的 event 操作
    for event in events_need_change:
        groups = list(hooks.get(event) or [])
        cleaned, _changed = _remove_ring_hooks_from_groups(groups)
        cleaned.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
        hooks[event] = cleaned

    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(_("✅ 已註冊 RiNG hooks（{events}）到 {path}", events=", ".join(events_need_change), path=settings))
    print("   " + _("重開 Claude Code session 後，🔴 待回覆狀態就會精準起來。"))
    return 0


def uninstall_hooks(dry_run: bool = False) -> int:
    """從 ~/.claude/settings.json 移除所有 RiNG hook 條目（對稱於 install_hooks）。"""
    set_lang(get_config().lang)
    settings = Path.home() / ".claude" / "settings.json"
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
    for event in _HOOK_EVENTS:
        groups = hooks.get(event)
        if not isinstance(groups, list):
            continue
        cleaned, was_removed = _remove_ring_hooks_from_groups(groups)
        if was_removed:
            removed.append(event)
            hooks[event] = cleaned

    if dry_run:
        print(f"# dry-run → {settings}\n")
        preview_hooks = {e: hooks.get(e, []) for e in _HOOK_EVENTS}
        print(json.dumps({"hooks": preview_hooks}, indent=2, ensure_ascii=False))
        return 0

    if not removed:
        print(_("ℹ️ 沒有找到 RiNG hook 條目，無需移除。"))
        return 0

    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(_("✅ 已移除 RiNG hooks（{events}）從 {path}", events=", ".join(removed), path=settings))
    return 0
