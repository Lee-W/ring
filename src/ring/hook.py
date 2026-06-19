"""RiNG 的 Claude Code hook handler——精準狀態的來源。

Claude Code 在各事件把一段 JSON 從 stdin 餵進來。我們據此 upsert 一份
``~/.config/ring/sessions/<session_id>.json``，讓 registry 讀到精準狀態
（zero-config 靠 mtime 猜不出「在等你」，這裡靠事件直接知道）。

設計原則：hook 永遠 exit 0、不擋住 session；解析失敗就安靜放行。

事件 → 狀態：
  SessionStart / UserPromptSubmit → 🟢 WORKING（剛開始 / 你剛回話，台上在跑）
  Stop / Notification             → 🔴 WAITING（回完一輪 = 待回覆 / 卡在權限提醒）
  SessionEnd                      → 刪檔（乾淨離場）
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from ring.config import get_config
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

_EVENT_STATUS = {
    "SessionStart": Status.WORKING,
    "UserPromptSubmit": Status.WORKING,
    "Notification": Status.WAITING,
    "Stop": Status.WAITING,
    "SessionEnd": Status.ENDED,
}

_HOOK_EVENTS = list(_EVENT_STATUS)


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


def _session_tty() -> str:
    """hook 是這個 session 的 claude 的後代——往上找到 claude，回它的控制終端 tty。

    這是「session → 哪個終端」最精準的對應（不必靠 cwd 猜），給 iTerm2 跳轉用。
    """
    pid = os.getppid()
    for _attempt in range(12):
        row = _ps_row(pid)
        if row is None:
            return ""
        ppid, comm = row
        if os.path.basename(comm.strip()) == "claude":
            return _pid_tty(pid)
        if ppid <= 1:
            return ""
        pid = ppid
    return ""


def run_hook() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0

    sid = data.get("session_id")
    status = _EVENT_STATUS.get(data.get("hook_event_name", ""))
    if not sid or status is None:
        return 0

    path = RING_REGISTRY / f"{sid}.json"
    if status is Status.ENDED:
        path.unlink(missing_ok=True)  # 乾淨離場：直接消失
        return 0

    last_action, todo = "—", None
    tp = data.get("transcript_path")
    if tp:
        records = _tail_records(Path(tp))
        if records:
            last_action = _latest_action(records)
            todo = _extract_todo(records)

    payload: dict[str, Any] = {
        "session_id": sid,
        "cwd": data.get("cwd", ""),
        "status": status.value,
        "last_active": time.time(),
        "last_action": last_action,
    }
    if todo:
        payload["todo"] = list(todo)
    tty = _session_tty()
    if tty:
        payload["tty"] = tty

    RING_REGISTRY.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(path)  # atomic
    return 0


def _ring_command() -> str:
    return f"{shutil.which('ring') or 'ring'} hook"


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
    added = []
    for event in _HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        already = any(h.get("command") == cmd for g in groups if isinstance(g, dict) for h in g.get("hooks", []))
        if already:
            continue
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 10}]})
        added.append(event)

    if dry_run:
        print(f"# dry-run → {settings}\n")
        print(json.dumps({"hooks": {e: hooks[e] for e in _HOOK_EVENTS}}, indent=2, ensure_ascii=False))
        return 0

    if not added:
        print(_("✅ RiNG hooks 已經裝過了，沒有變更。"))
        return 0

    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    print(_("✅ 已註冊 RiNG hooks（{events}）到 {path}", events=", ".join(added), path=settings))
    print("   " + _("重開 Claude Code session 後，🔴 待回覆狀態就會精準起來。"))
    return 0
