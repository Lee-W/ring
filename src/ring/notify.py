"""系統通知模組——把「新轉為等你」的 session 發給使用者。

優先使用 terminal-notifier（brew 安裝的外部 binary），帶點擊回呼
``ring focus <session_id>`` 讓使用者點擊後直接聚焦到對應終端。
未裝 terminal-notifier 則退化為 osascript 純文字通知（點擊不可聚焦）。

安裝 terminal-notifier（取得點擊跳轉能力）：
    brew install terminal-notifier

通知失敗一律安靜吞掉——通知是錦上添花，絕不打斷主流程。
terminal-notifier 不進 pyproject dependencies（brew 外部 binary）。
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ring.config import get_config
from ring.focus.base import osascript
from ring.i18n import gettext as _
from ring.registry import Session

# 一次性安裝引導的 marker 檔路徑。
_HINT_MARKER: Path = Path.home() / ".config" / "ring" / ".tn-hint-shown"


def _cwd_tail(session: Session) -> str:
    """取 cwd 最後一段（尾目錄），用於通知的區分資訊。"""
    return Path(session.cwd).name or session.cwd


def _ring_executable() -> str:
    """回傳可被 terminal-notifier click callback 執行的 ring 路徑。

    macOS 從通知中心觸發 ``-execute`` 時不一定有使用者 shell 的 PATH；通知建立時
    先解析成絕對路徑，點擊才不會因找不到 ``ring`` 而失效。
    """
    current = Path(sys.argv[0])
    if current.is_absolute() and current.exists():
        return str(current)
    found = shutil.which("ring")
    return found or "ring"


def _ring_focus_command(session_id: str) -> str:
    return f"{shlex.quote(_ring_executable())} focus {shlex.quote(session_id)}"


def notify_waiting(sessions: list[Session]) -> str | None:
    """對一批「新轉為等你」的 session 發系統通知。

    - 有 terminal-notifier → 每筆各發一則，帶 ``-execute "ring focus <session_id>"``。
    - 無 terminal-notifier → fallback osascript display notification（純文字，逐 session 各一則）。
    - 首次走 osascript 路徑時，回傳安裝引導提示字串（由呼叫方決定怎麼呈現）；之後回傳 ``None``。
    - 失敗一律安靜吞掉，不拋例外。

    :param sessions: 新轉為 waiting 的 session 清單；空清單時直接回傳。
    :returns: 安裝引導提示字串（首次走 osascript 路徑時）或 ``None``。
    """
    if not sessions:
        return None

    backend = get_config().notify_backend
    has_tn = bool(shutil.which("terminal-notifier"))
    # terminal-notifier 支援點擊跳轉，但有些 macOS 會默默擋掉它的通知；那種機器把
    # notify_backend 設成 "osascript" 就能改用看得到的純文字通知（代價：點擊不跳轉）。
    if backend == "terminal-notifier" or (backend == "auto" and has_tn):
        _notify_with_terminal_notifier(sessions)
        return None
    hint = _maybe_show_install_hint() if (backend == "auto" and not has_tn) else None
    _notify_with_osascript(sessions)
    return hint


def _notify_with_terminal_notifier(sessions: list[Session]) -> None:
    """每個 session 各發一則 terminal-notifier 通知，帶點擊聚焦回呼。"""
    cfg = get_config()
    for s in sessions:
        tail = _cwd_tail(s)
        message = _("{project} · …/{tail}", project=s.project, tail=tail)
        cmd = [
            "terminal-notifier",
            "-title",
            _("RiNG · {project} 在等你回話", project=s.project),
            "-message",
            message,
            "-execute",
            _ring_focus_command(s.session_id),
        ]
        if cfg.notify_sound:
            cmd.extend(["-sound", cfg.notify_sound_name or "default"])
        try:
            subprocess.run(
                cmd,
                capture_output=True,
                timeout=10,
            )
        except Exception:
            pass


def _notify_with_osascript(sessions: list[Session]) -> None:
    """用 osascript 逐 session 各發一則純文字通知（fallback，點擊不可聚焦）。"""
    cfg = get_config()
    for s in sessions:
        tail = _cwd_tail(s)
        message = _("{project} · …/{tail}", project=s.project, tail=tail)
        title = _("RiNG · {project} 在等你回話", project=s.project)
        sound = f' sound name "{cfg.notify_sound_name}"' if cfg.notify_sound else ""
        try:
            osascript(f'display notification "{message}" with title "{title}"{sound}')
        except Exception:
            pass


def _maybe_show_install_hint() -> str | None:
    """首次走 osascript 路徑時，回傳 terminal-notifier 安裝建議字串（marker 檔防重複）。

    :returns: hint 字串（首次）或 ``None``（已提示過或寫 marker 失敗時）。
    """
    if _HINT_MARKER.exists():
        return None
    try:
        hint = _("💡 裝 terminal-notifier 可點擊通知直接跳回 session：brew install terminal-notifier")
        _HINT_MARKER.parent.mkdir(parents=True, exist_ok=True)
        _HINT_MARKER.touch()
        return hint
    except Exception:
        return None
