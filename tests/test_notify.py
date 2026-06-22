"""notify_waiting 測試——mock shutil.which + subprocess / osascript。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ring.notify import notify_waiting
from ring.registry import Session, Status


def _s(sid: str, project_name: str = "proj") -> Session:
    return Session(sid, f"/x/{project_name}", Status.WAITING, 0.0, "→ Edit", "hook")


class TestNotifyWithTerminalNotifier:
    def test_uses_terminal_notifier_when_available(self) -> None:
        """有 terminal-notifier → 走 terminal-notifier 路徑。"""
        session = _s("uuid-1", "maigo")
        with patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"), patch(
            "subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "terminal-notifier"

    def test_execute_contains_ring_focus_session_id(self) -> None:
        """-execute 參數含 ring focus <session_id>。"""
        session = _s("test-uuid-123", "proj")
        with patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"), patch(
            "subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])
        args = mock_run.call_args[0][0]
        # 找 -execute 參數值
        execute_idx = args.index("-execute")
        execute_val = args[execute_idx + 1]
        assert "ring focus test-uuid-123" in execute_val

    def test_each_session_gets_own_notification(self) -> None:
        """多個 session 時，每筆各發一則通知。"""
        sessions = [_s("uuid-1", "proj1"), _s("uuid-2", "proj2")]
        with patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"), patch(
            "subprocess.run"
        ) as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting(sessions)
        assert mock_run.call_count == 2

    def test_subprocess_failure_is_silently_swallowed(self) -> None:
        """subprocess 拋例外 → 被吞、無例外外漏。"""
        session = _s("uuid-1")
        with patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"), patch(
            "subprocess.run", side_effect=Exception("subprocess error")
        ):
            notify_waiting([session])  # 不應拋例外

    def test_timeout_error_silently_swallowed(self) -> None:
        """subprocess TimeoutExpired → 被吞。"""
        import subprocess

        session = _s("uuid-1")
        with patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"), patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="terminal-notifier", timeout=10)
        ):
            notify_waiting([session])  # 不應拋例外


class TestNotifyWithOsascript:
    def test_falls_back_to_osascript_when_no_terminal_notifier(self) -> None:
        """無 terminal-notifier → fallback osascript 路徑。"""
        session = _s("uuid-1", "maigo")
        with patch("shutil.which", return_value=None), patch(
            "ring.notify.osascript"
        ) as mock_osa:
            mock_osa.return_value = (0, "", "")
            notify_waiting([session])
        mock_osa.assert_called_once()
        script = mock_osa.call_args[0][0]
        assert "display notification" in script

    def test_osascript_failure_silently_swallowed(self) -> None:
        """osascript 拋例外 → 被吞、無例外外漏。"""
        session = _s("uuid-1")
        with patch("shutil.which", return_value=None), patch(
            "ring.notify.osascript", side_effect=Exception("osa error")
        ):
            notify_waiting([session])  # 不應拋例外


class TestNotifyEmpty:
    def test_empty_sessions_does_nothing(self) -> None:
        """空清單直接回傳，不呼叫任何通知機制。"""
        with patch("shutil.which") as mock_which, patch("subprocess.run") as mock_run, patch(
            "ring.notify.osascript"
        ) as mock_osa:
            notify_waiting([])
        mock_which.assert_not_called()
        mock_run.assert_not_called()
        mock_osa.assert_not_called()
