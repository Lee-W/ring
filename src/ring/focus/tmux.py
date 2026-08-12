"""tmux focuser：同一個 tmux server 直接 switch-client 切到那個 pane。

成功後不中止 dispatcher（``continue_after_success``）：tmux 只能切 pane，換不了外層終端
視窗本身有沒有浮到前景，所以讓 iTerm2 / Terminal.app / kitty / linux-wm 等外層 focuser
接著跑，才有機會把視窗帶到前景（設計依據見 ``focus/base.py:8-9``）。
"""

from __future__ import annotations

import shutil
import subprocess

from ring.registry import Session


def _client_tty(tmux_session: str, target: str) -> str:
    """查這個 tmux session 目前的 client tty，回填給外層 focuser 配對用。

    多個 client attach 同一個 session 時取第一個非空值；查不到（沒有 client、指令失敗、
    逾時）一律回空字串，呼叫端不得把這種情況當成 switch-client 失敗。
    """
    for cmd in (
        ["tmux", "list-clients", "-t", tmux_session, "-F", "#{client_tty}"],
        ["tmux", "display-message", "-p", "-t", target, "#{client_tty}"],
    ):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        except (OSError, subprocess.SubprocessError):
            continue
        if result.returncode != 0:
            continue
        for line in result.stdout.splitlines():
            line = line.strip()
            if line:
                return line
    return ""


class TmuxFocuser:
    name = "tmux"
    continue_after_success = True

    def try_focus(self, session: Session) -> tuple[bool, str] | None:
        target = session.tmux_target
        if not target or not shutil.which("tmux"):
            return None
        tmux_session = target.split(":", 1)[0]
        window = target.split(".", 1)[0]
        ok = False
        last_err = ""
        for cmd in (
            ["tmux", "switch-client", "-t", tmux_session],
            ["tmux", "select-window", "-t", window],
            ["tmux", "select-pane", "-t", target],
        ):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
            except (OSError, subprocess.SubprocessError) as exc:
                return False, str(exc)
            if result.returncode == 0:
                ok = True
            else:
                last_err = result.stderr.strip()
        if not ok:
            return False, last_err or "tmux switch failed"
        # switch-client 之後，接上這個 tmux session 的 client 的 tty 就是那個終端視窗的 tty。
        # 回填給後面的外層 focuser 配對用（同 neovim.py:176-179 的做法）。
        client_tty = _client_tty(tmux_session, target)
        if client_tty.startswith("/dev/"):
            session.tty = client_tty
        return True, f"tmux {target}"


focuser = TmuxFocuser()
