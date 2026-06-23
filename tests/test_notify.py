"""notify_waiting 測試——mock shutil.which + subprocess / osascript。"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from ring.config import Config
from ring.notify import notify_waiting
from ring.registry import Session, Status


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """讓 notify 測試不受機器上 ~/.config/ring/config.toml 影響（預設 backend=auto）。

    需要特定 config 的測試仍可在內部用 ``with patch("ring.notify.get_config", ...)`` 覆寫。
    """
    monkeypatch.setattr("ring.notify.get_config", lambda: Config())


def _which_only(*available: str) -> Callable[[str], str | None]:
    """模擬 shutil.which：只有指定的 binary「裝了」，其餘回 None。"""
    avail = set(available)

    def _w(cmd: str) -> str | None:
        return f"/usr/bin/{cmd}" if cmd in avail else None

    return _w


def _s(sid: str, project_name: str = "proj", cwd: str | None = None) -> Session:
    resolved_cwd = cwd or f"/x/{project_name}"
    return Session(sid, resolved_cwd, Status.WAITING, 0.0, "→ Edit", "hook", origin_cwd=f"/x/{project_name}")


class TestNotifyWithTerminalNotifier:
    def test_uses_terminal_notifier_when_available(self) -> None:
        """有 terminal-notifier → 走 terminal-notifier 路徑。"""
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])
        mock_run.assert_called_once()
        args = mock_run.call_args[0][0]
        assert args[0] == "terminal-notifier"

    def test_execute_contains_ring_focus_session_id(self) -> None:
        """-execute 參數含 ring focus <session_id>。"""
        session = _s("test-uuid-123", "proj")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("ring.notify._ring_executable", return_value="/opt/ring/bin/ring"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])
        args = mock_run.call_args[0][0]
        # 找 -execute 參數值
        execute_idx = args.index("-execute")
        execute_val = args[execute_idx + 1]
        assert execute_val == "/opt/ring/bin/ring focus test-uuid-123"

    def test_execute_quotes_session_id(self) -> None:
        """session id 可能含 provider prefix；click callback 要安全 quote。"""
        session = _s("codex:session with space", "proj")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("ring.notify._ring_executable", return_value="/opt/ring/bin/ring"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])

        args = mock_run.call_args[0][0]
        execute_val = args[args.index("-execute") + 1]
        assert execute_val == "/opt/ring/bin/ring focus 'codex:session with space'"

    def test_each_session_gets_own_notification(self) -> None:
        """多個 session 時，每筆各發一則通知。"""
        sessions = [_s("uuid-1", "proj1"), _s("uuid-2", "proj2")]
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting(sessions)
        assert mock_run.call_count == 2

    def test_subprocess_failure_is_silently_swallowed(self) -> None:
        """subprocess 拋例外 → 被吞、無例外外漏。"""
        session = _s("uuid-1")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("subprocess.run", side_effect=Exception("subprocess error")),
        ):
            notify_waiting([session])  # 不應拋例外

    def test_timeout_error_silently_swallowed(self) -> None:
        """subprocess TimeoutExpired → 被吞。"""
        import subprocess

        session = _s("uuid-1")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="terminal-notifier", timeout=10)),
        ):
            notify_waiting([session])  # 不應拋例外

    def test_title_contains_project(self) -> None:
        """terminal-notifier 的 -title 包含 project 名稱。"""
        session = _s("uuid-1", "myproject")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])
        args = mock_run.call_args[0][0]
        title_idx = args.index("-title")
        title_val = args[title_idx + 1]
        assert "myproject" in title_val

    def test_message_contains_project_and_tail(self) -> None:
        """terminal-notifier 的 -message 包含 project + cwd 末段（兩者各自驗）。"""
        # cwd 末段（checkout-123）刻意與 project name（myproject）不同，確保兩個 assert 各自獨立。
        session = _s("uuid-1", "myproject", cwd="/home/user/work/checkout-123")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])
        args = mock_run.call_args[0][0]
        message_idx = args.index("-message")
        message_val = args[message_idx + 1]
        assert "myproject" in message_val  # project name
        assert "checkout-123" in message_val  # tail = cwd 末段

    def test_terminal_notifier_uses_sound_when_enabled(self) -> None:
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("ring.notify.get_config", return_value=Config(notify_sound=True, notify_sound_name="Glass")),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])

        args = mock_run.call_args[0][0]
        assert args[args.index("-sound") + 1] == "Glass"

    def test_terminal_notifier_omits_sound_when_disabled(self) -> None:
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("ring.notify.get_config", return_value=Config(notify_sound=False)),
            patch("subprocess.run") as mock_run,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])

        args = mock_run.call_args[0][0]
        assert "-sound" not in args


class TestNotifyWithOsascript:
    def test_falls_back_to_osascript_when_no_terminal_notifier(self) -> None:
        """無 terminal-notifier → fallback osascript 路徑。"""
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", side_effect=_which_only("osascript")),
            patch("ring.notify.osascript") as mock_osa,
            patch("ring.notify._HINT_MARKER") as mock_marker,
        ):
            mock_osa.return_value = (0, "", "")
            mock_marker.exists.return_value = True  # suppress hint
            notify_waiting([session])
        mock_osa.assert_called_once()
        script = mock_osa.call_args[0][0]
        assert "display notification" in script

    def test_osascript_each_session_gets_own_call(self) -> None:
        """無 terminal-notifier 時，多 session 各自發一則 osascript（不再合併）。"""
        sessions = [_s("uuid-1", "proj1"), _s("uuid-2", "proj2")]
        with (
            patch("shutil.which", side_effect=_which_only("osascript")),
            patch("ring.notify.osascript") as mock_osa,
            patch("ring.notify._HINT_MARKER") as mock_marker,
        ):
            mock_osa.return_value = (0, "", "")
            mock_marker.exists.return_value = True  # suppress hint
            notify_waiting(sessions)
        assert mock_osa.call_count == 2

    def test_osascript_message_contains_project_and_tail(self) -> None:
        """osascript 的 script 包含 project + cwd 末段。"""
        session = _s("uuid-1", "myproject", cwd="/home/user/repos/myproject")
        with (
            patch("shutil.which", side_effect=_which_only("osascript")),
            patch("ring.notify.osascript") as mock_osa,
            patch("ring.notify._HINT_MARKER") as mock_marker,
        ):
            mock_osa.return_value = (0, "", "")
            mock_marker.exists.return_value = True  # suppress hint
            notify_waiting([session])
        script = mock_osa.call_args[0][0]
        assert "myproject" in script

    def test_osascript_uses_sound_when_enabled(self) -> None:
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", side_effect=_which_only("osascript")),
            patch("ring.notify.get_config", return_value=Config(notify_sound=True, notify_sound_name="Glass")),
            patch("ring.notify.osascript") as mock_osa,
            patch("ring.notify._HINT_MARKER") as mock_marker,
        ):
            mock_osa.return_value = (0, "", "")
            mock_marker.exists.return_value = True
            notify_waiting([session])

        script = mock_osa.call_args[0][0]
        assert 'sound name "Glass"' in script

    def test_osascript_failure_silently_swallowed(self) -> None:
        """osascript 拋例外 → 被吞、無例外外漏。"""
        session = _s("uuid-1")
        with (
            patch("shutil.which", side_effect=_which_only("osascript")),
            patch("ring.notify.osascript", side_effect=Exception("osa error")),
            patch("ring.notify._HINT_MARKER") as mock_marker,
        ):
            mock_marker.exists.return_value = True
            notify_waiting([session])  # 不應拋例外


class TestInstallHint:
    def test_hint_shown_once(self, tmp_path: Path) -> None:
        """首次走 osascript → 回傳 hint 字串；之後不再重複（marker 防重複）。"""
        import ring.notify as notify_mod
        from ring.notify import _maybe_show_install_hint

        marker_path = tmp_path / ".tn-hint-shown"
        original = notify_mod._HINT_MARKER
        notify_mod._HINT_MARKER = marker_path
        try:
            first = _maybe_show_install_hint()
            second = _maybe_show_install_hint()

            assert first is not None
            assert "terminal-notifier" in first
            assert second is None  # 第二次 marker 已存在，回傳 None
            assert marker_path.exists()
        finally:
            notify_mod._HINT_MARKER = original

    def test_hint_not_shown_when_marker_exists(self, tmp_path: Path) -> None:
        """marker 已存在 → 回傳 None（不重複提示）。"""
        import ring.notify as notify_mod
        from ring.notify import _maybe_show_install_hint

        marker_path = tmp_path / ".tn-hint-shown"
        marker_path.touch()
        original = notify_mod._HINT_MARKER
        notify_mod._HINT_MARKER = marker_path
        try:
            result = _maybe_show_install_hint()
            assert result is None
        finally:
            notify_mod._HINT_MARKER = original


class TestNotifyBackend:
    def test_osascript_backend_forces_osascript_even_with_terminal_notifier(self) -> None:
        """notify_backend="osascript" → 即使裝了 terminal-notifier 也走 osascript。"""
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("ring.notify.get_config", return_value=Config(notify_backend="osascript")),
            patch("ring.notify.osascript") as mock_osa,
            patch("subprocess.run") as mock_run,
            patch("ring.notify._HINT_MARKER") as mock_marker,
        ):
            mock_osa.return_value = (0, "", "")
            mock_marker.exists.return_value = True
            notify_waiting([session])
        mock_osa.assert_called_once()  # 走 osascript
        mock_run.assert_not_called()  # 不碰 terminal-notifier

    def test_osascript_backend_returns_no_install_hint(self) -> None:
        """明確選 osascript → 不回傳「裝 terminal-notifier」的提示。"""
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", side_effect=_which_only("osascript")),
            patch("ring.notify.get_config", return_value=Config(notify_backend="osascript")),
            patch("ring.notify.osascript", return_value=(0, "", "")),
        ):
            assert notify_waiting([session]) is None

    def test_terminal_notifier_backend_forces_tn(self) -> None:
        """notify_backend="terminal-notifier" → 走 terminal-notifier。"""
        session = _s("uuid-1", "maigo")
        with (
            patch("shutil.which", return_value="/usr/local/bin/terminal-notifier"),
            patch("ring.notify.get_config", return_value=Config(notify_backend="terminal-notifier")),
            patch("subprocess.run") as mock_run,
            patch("ring.notify.osascript") as mock_osa,
        ):
            mock_run.return_value = MagicMock(returncode=0)
            notify_waiting([session])
        mock_run.assert_called_once()
        mock_osa.assert_not_called()


class _FakeNotifier:
    def __init__(self, name: str, *, available: bool = True, click: bool = False) -> None:
        self.name = name
        self._available = available
        self._click = click
        self.sent: list[list[Session]] = []

    def available(self) -> bool:
        return self._available

    def supports_click(self) -> bool:
        return self._click

    def send(self, sessions: list[Session]) -> None:
        self.sent.append(list(sessions))


class TestNotifierAbstraction:
    """可插拔 Notifier registry——泛用、不綁特定平台。"""

    def test_auto_prefers_clickable_available(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plain = _FakeNotifier("plain", click=False)
        clicky = _FakeNotifier("clicky", click=True)
        monkeypatch.setattr("ring.notify._NOTIFIERS", [plain, clicky])
        monkeypatch.setattr("ring.notify.get_config", lambda: Config(notify_backend="auto"))
        notify_waiting([_s("x")])
        assert clicky.sent and not plain.sent

    def test_auto_falls_to_non_clickable_when_no_clickable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plain = _FakeNotifier("plain", click=False)
        monkeypatch.setattr("ring.notify._NOTIFIERS", [plain])
        monkeypatch.setattr("ring.notify.get_config", lambda: Config(notify_backend="auto"))
        notify_waiting([_s("x")])
        assert plain.sent

    def test_explicit_backend_used(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plain = _FakeNotifier("plain")
        clicky = _FakeNotifier("clicky", click=True)
        monkeypatch.setattr("ring.notify._NOTIFIERS", [plain, clicky])
        monkeypatch.setattr("ring.notify.get_config", lambda: Config(notify_backend="plain"))
        notify_waiting([_s("x")])
        assert plain.sent and not clicky.sent

    def test_unknown_backend_falls_back_to_auto(self, monkeypatch: pytest.MonkeyPatch) -> None:
        only = _FakeNotifier("only", click=True)
        monkeypatch.setattr("ring.notify._NOTIFIERS", [only])
        monkeypatch.setattr("ring.notify.get_config", lambda: Config(notify_backend="does-not-exist"))
        notify_waiting([_s("x")])
        assert only.sent  # 認不得的後端 → 退回 auto → 唯一可用

    def test_backend_none_disables_all_notifications(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """notify_backend="none" → 完全不發，即使有可用後端（RiNG 當純看板）。"""
        clicky = _FakeNotifier("clicky", click=True)
        plain = _FakeNotifier("plain")
        monkeypatch.setattr("ring.notify._NOTIFIERS", [clicky, plain])
        monkeypatch.setattr("ring.notify.get_config", lambda: Config(notify_backend="none"))
        assert notify_waiting([_s("x")]) is None
        assert not clicky.sent and not plain.sent

    def test_no_available_notifier_sends_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        dead = _FakeNotifier("dead", available=False)
        monkeypatch.setattr("ring.notify._NOTIFIERS", [dead])
        monkeypatch.setattr("ring.notify.get_config", lambda: Config())
        assert notify_waiting([_s("x")]) is None
        assert not dead.sent

    def test_register_notifier_extends_registry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from ring.notify import notifiers, register_notifier

        monkeypatch.setattr("ring.notify._NOTIFIERS", [])
        custom = _FakeNotifier("custom")
        register_notifier(custom)
        assert notifiers()[-1] is custom
        first = _FakeNotifier("first")
        register_notifier(first, first=True)
        assert notifiers()[0] is first


class TestNotifyEmpty:
    def test_empty_sessions_does_nothing(self) -> None:
        """空清單直接回傳，不呼叫任何通知機制。"""
        with (
            patch("shutil.which") as mock_which,
            patch("subprocess.run") as mock_run,
            patch("ring.notify.osascript") as mock_osa,
        ):
            notify_waiting([])
        mock_which.assert_not_called()
        mock_run.assert_not_called()
        mock_osa.assert_not_called()
