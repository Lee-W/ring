"""tmux focuser：同一個 tmux server 直接 switch-client 切到那個 pane。"""

from __future__ import annotations

import shutil
import subprocess

from ring.registry import Session


class TmuxFocuser:
    name = "tmux"

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
        return (True, f"tmux {target}") if ok else (False, last_err or "tmux switch failed")


focuser = TmuxFocuser()
