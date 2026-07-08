from pathlib import Path
from unittest.mock import patch

import pytest

import ring.cli as cli
from ring.registry import Session, Status


def _sessions() -> list[Session]:
    return [Session("a", "/x/maigo", Status.WORKING, 0.0, "→ Edit", "scan")]


def test_main_snapshot_en(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: _sessions())
    monkeypatch.setattr(cli, "running_agent_pids", lambda: [1])
    rc = cli.main(["--lang", "en", "--no-legend"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "on stage" in out
    assert "maigo" in out


def test_main_snapshot_default_is_zh(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: _sessions())
    monkeypatch.setattr(cli, "running_agent_pids", lambda: [1])
    rc = cli.main(["--no-legend"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "在場" in out  # 預設台灣漢語


def test_main_empty_board(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: [])
    monkeypatch.setattr(cli, "running_agent_pids", lambda: [])
    assert cli.main(["--lang", "en"]) == 0
    assert "stage" in capsys.readouterr().out


def test_peek_lang() -> None:
    assert cli._peek_lang(["--lang", "en"]) == "en"
    assert cli._peek_lang(["--lang=zh-Hant"]) == "zh-Hant"
    assert cli._peek_lang(["--watch"]) is None


def test_version_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])


def test_help_lists_hidden_commands(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    out = capsys.readouterr().out
    assert "install-hooks" in out
    assert "remove-hooks" in out
    assert "config" in out
    assert "hook --provider" in out
    assert "focus SESSION_ID" in out
    assert "gc" in out
    assert "doctor" in out
    assert "digest" in out


def test_config_shows_path_and_settings(capsys: pytest.CaptureFixture[str]) -> None:
    """ring config 印出設定檔路徑與目前生效的設定欄位。"""
    rc = cli.main(["config", "--lang", "en"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(cli.CONFIG_PATH) in out
    assert "notify_backend" in out  # dataclass 欄位都列出來
    assert "Effective settings" in out


def test_config_marks_overridden_values(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """跟內建預設不同的值要標 ←，預設值不標。"""
    monkeypatch.setattr(cli, "get_config", lambda: cli.Config(notify_backend="agent-hooks"))
    cli.main(["config", "--lang", "en"])
    lines = capsys.readouterr().out.splitlines()
    backend_line = next(line for line in lines if "notify_backend" in line)
    interval_line = next(line for line in lines if "interval" in line)
    assert "←" in backend_line  # 覆寫過
    assert "←" not in interval_line  # 維持預設


def test_config_help_does_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(cli, "print_config") as mock_cfg:
        rc = cli.main(["config", "--help"])
    assert rc == 0
    mock_cfg.assert_not_called()
    assert "usage: ring config" in capsys.readouterr().out


def test_config_get_prints_value(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "get_config", lambda: cli.Config(notify_backend="agent-hooks"))
    rc = cli.main(["config", "get", "notify_backend"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "agent-hooks"


def test_config_get_unknown_key_errors(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["config", "get", "bogus"])
    assert rc == 1
    assert "bogus" in capsys.readouterr().err


def test_config_set_routes_to_set_value(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(cli, "set_value", return_value="agent-hooks") as mock_set:
        rc = cli.main(["config", "set", "notify_backend", "agent-hooks"])
    assert rc == 0
    mock_set.assert_called_once_with("notify_backend", "agent-hooks")
    assert "notify_backend" in capsys.readouterr().out


def test_config_set_wrong_arity_errors() -> None:
    assert cli.main(["config", "set", "notify_backend"]) == 2


def test_config_unknown_action_errors() -> None:
    assert cli.main(["config", "frobnicate"]) == 2


def test_install_hooks_help_does_not_install(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("ring.hook.install_hooks") as mock_install:
        rc = cli.main(["install-hooks", "--help"])

    assert rc == 0
    mock_install.assert_not_called()
    assert "usage: ring install-hooks" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# remove-hooks 路由
# ---------------------------------------------------------------------------


def test_remove_hooks_routes_to_uninstall(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove-hooks 路由到 uninstall_hooks。"""
    called: list[dict[str, object]] = []

    def fake_uninstall(dry_run: bool = False) -> int:
        called.append({"dry_run": dry_run})
        return 0

    with patch("ring.hook.uninstall_hooks", fake_uninstall):
        rc = cli.main(["remove-hooks"])
    assert rc == 0
    assert called == [{"dry_run": False}]


def test_remove_hooks_dry_run_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """remove-hooks --dry-run 路由正確傳 dry_run=True。"""
    called: list[dict[str, object]] = []

    def fake_uninstall(dry_run: bool = False) -> int:
        called.append({"dry_run": dry_run})
        return 0

    with patch("ring.hook.uninstall_hooks", fake_uninstall):
        rc = cli.main(["remove-hooks", "--dry-run"])
    assert rc == 0
    assert called == [{"dry_run": True}]


# ---------------------------------------------------------------------------
# hook provider 路由
# ---------------------------------------------------------------------------


def test_hook_provider_flag_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """hook --provider codex 會把 provider 傳給 run_hook。"""
    called: list[str] = []

    def fake_run_hook(provider: str = "claude-code") -> int:
        called.append(provider)
        return 0

    with patch("ring.hook.run_hook", fake_run_hook):
        rc = cli.main(["hook", "--provider", "codex"])

    assert rc == 0
    assert called == ["codex"]


def test_hook_provider_positional_routes(monkeypatch: pytest.MonkeyPatch) -> None:
    """hook codex 是 --provider codex 的簡寫。"""
    called: list[str] = []

    def fake_run_hook(provider: str = "claude-code") -> int:
        called.append(provider)
        return 0

    with patch("ring.hook.run_hook", fake_run_hook):
        rc = cli.main(["hook", "codex"])

    assert rc == 0
    assert called == ["codex"]


# ---------------------------------------------------------------------------
# focus <session_id> 路由
# ---------------------------------------------------------------------------


def test_focus_headless_calls_focus_jump(monkeypatch: pytest.MonkeyPatch) -> None:
    """focus <uuid> headless（無 TUI presence）→ 呼叫 focus.jump（現行退化行為）。"""
    session = Session("test-uuid", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")
    jump_called: list[Session] = []

    def fake_get_by_id(sid: str) -> Session | None:
        return session if sid == "test-uuid" else None

    def fake_jump(s: Session) -> tuple[bool, str]:
        jump_called.append(s)
        return True, "jumped"

    # read_tui_presence 回 None → headless
    with (
        patch("ring.sources.get_by_id", fake_get_by_id),
        patch("ring.focus.jump", fake_jump),
        patch("ring.ipc.read_tui_presence", return_value=None),
    ):
        rc = cli.main(["focus", "test-uuid"])
    assert rc == 0
    assert jump_called == [session]


def test_focus_tui_running_writes_request(tmp_path: Path) -> None:
    """focus <uuid> 且 TUI 在跑 → 寫 focus-request，不直接呼叫 focus_jump。"""
    session = Session("tui-uuid", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")
    jump_called: list[Session] = []
    req_path = tmp_path / "focus-request"

    def fake_get_by_id(sid: str) -> Session | None:
        return session if sid == "tui-uuid" else None

    def fake_jump(s: Session) -> tuple[bool, str]:
        jump_called.append(s)
        return True, "jumped"

    fake_presence = {"tty": "/dev/ttys001", "pid": 1234, "ts": 0.0}

    def fake_write_request(sid: str, *, request_path: Path | None = None) -> None:
        p = request_path or req_path
        import json

        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"session_id": sid, "ts": 0.0}), encoding="utf-8")

    with (
        patch("ring.sources.get_by_id", fake_get_by_id),
        patch("ring.focus.jump", fake_jump),
        patch("ring.ipc.read_tui_presence", return_value=fake_presence),
        patch("ring.ipc.write_focus_request", fake_write_request),
    ):
        rc = cli.main(["focus", "tui-uuid"])
    assert rc == 0
    assert jump_called == [], "TUI 在跑時不應直接呼叫 focus_jump"


def test_focus_not_found_returns_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """focus <uuid> 查不到 → 清楚報錯並回 non-zero。"""
    jump_called: list[Session] = []

    def fake_get_by_id(sid: str) -> Session | None:
        return None

    def fake_jump(s: Session) -> tuple[bool, str]:
        jump_called.append(s)
        return True, "jumped"

    with (
        patch("ring.sources.get_by_id", fake_get_by_id),
        patch("ring.sources.discover_sessions", return_value=[]),
        patch("ring.focus.jump", fake_jump),
    ):
        rc = cli.main(["focus", "nonexistent-uuid"])
    assert rc == 1
    assert jump_called == [], "查不到時不應呼叫 jump"
    assert "nonexistent-uuid" in capsys.readouterr().err


def test_focus_unique_prefix_matches_session(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session("abcdef", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")
    jumped: list[Session] = []

    def fake_jump(s: Session) -> tuple[bool, str]:
        jumped.append(s)
        return True, "jumped"

    with (
        patch("ring.sources.get_by_id", return_value=None),
        patch("ring.sources.discover_sessions", return_value=[session]),
        patch("ring.ipc.read_tui_presence", return_value=None),
        patch("ring.focus.jump", fake_jump),
    ):
        rc = cli.main(["focus", "abc"])

    assert rc == 0
    assert jumped == [session]


def test_focus_ambiguous_prefix_returns_error(capsys: pytest.CaptureFixture[str]) -> None:
    sessions = [
        Session("abc111", "/x/one", Status.WAITING, 0.0, "→ Edit", "hook"),
        Session("abc222", "/x/two", Status.WAITING, 0.0, "→ Edit", "hook"),
    ]

    with (
        patch("ring.sources.get_by_id", return_value=None),
        patch("ring.sources.discover_sessions", return_value=sessions),
    ):
        rc = cli.main(["focus", "abc"])

    assert rc == 2
    err = capsys.readouterr().err
    assert "abc111" in err
    assert "abc222" in err


def test_focus_missing_arg_returns_usage(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["focus"]) == 2
    assert "ring focus SESSION_ID" in capsys.readouterr().err


def test_focus_jump_failure_returns_error(capsys: pytest.CaptureFixture[str]) -> None:
    session = Session("test-uuid", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")
    with (
        patch("ring.sources.get_by_id", return_value=session),
        patch("ring.ipc.read_tui_presence", return_value=None),
        patch("ring.focus.jump", return_value=(False, "no focuser")),
    ):
        rc = cli.main(["focus", "test-uuid"])

    assert rc == 1
    assert "no focuser" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# doctor 子命令
# ---------------------------------------------------------------------------


def _make_fake_source(name: str, sessions: list[Session] | Exception) -> object:
    """製作 fake SessionSource（discover 回固定 list 或拋例外）。"""

    class FakeSource:
        def __init__(self, n: str, result: list[Session] | Exception) -> None:
            self.name = n
            self._result = result

        def discover(self) -> list[Session]:
            if isinstance(self._result, Exception):
                raise self._result
            return self._result

    return FakeSource(name, sessions)


def _make_fake_notifier(name: str, available: bool, supports_click: bool = False) -> object:
    """製作 fake Notifier（available 固定值）。"""
    import types

    nt = types.SimpleNamespace(
        name=name,
        available=lambda: available,
        supports_click=lambda: supports_click,
        send=lambda sessions: None,
    )
    return nt


def _make_fake_focuser(name: str) -> object:
    """製作 fake Focuser（name 固定值，try_focus 返回 None）。"""
    import types

    return types.SimpleNamespace(
        name=name,
        try_focus=lambda session: None,
    )


def _make_fake_hook_status(
    claude_installed: bool = True, codex_applicable: bool = False, codex_installed: bool = False
) -> list[object]:
    from ring.hook import HookStatus

    home = Path.home()
    return [
        HookStatus(
            provider="claude-code",
            path=home / ".claude" / "settings.json",
            applicable=True,
            installed=claude_installed,
            exists=claude_installed,
        ),
        HookStatus(
            provider="codex",
            path=home / ".codex" / "hooks.json",
            applicable=codex_applicable,
            installed=codex_installed,
            exists=codex_installed,
        ),
    ]


def test_doctor_returns_zero_and_shows_sections(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ring doctor 回 0；五節標題都在輸出。"""
    fake_src = _make_fake_source("hook", [])
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])

    fake_nt = _make_fake_notifier("terminal-notifier", True, True)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [fake_nt])

    fake_focuser = _make_fake_focuser("tmux")
    monkeypatch.setattr("ring.focus._FOCUSERS", [fake_focuser])

    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)

    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: "/usr/bin/tmux" if name == "tmux" else None)
        rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    # 五節標題（繁體中文 msgid）
    assert "RiNG 環境診斷" in out
    assert "Session 來源" in out
    assert "Hook 安裝" in out
    assert "Hook 心跳偵測" in out
    assert "通知後端" in out
    assert "聚焦終端" in out
    assert "維護" in out
    assert "設定檔" in out


def test_doctor_counts_sessions_per_source(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """fake source discover 回 3 個 Session → output 含 3。"""
    fake_sessions = [Session(f"s{i}", "/x", Status.IDLE, 0.0, "—", "hook") for i in range(3)]
    fake_src = _make_fake_source("hook", fake_sessions)
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])

    fake_nt = _make_fake_notifier("terminal-notifier", True, True)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [fake_nt])

    fake_focuser = _make_fake_focuser("tmux")
    monkeypatch.setattr("ring.focus._FOCUSERS", [fake_focuser])

    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)

    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: None)
        rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "3" in out


def test_doctor_source_failure_is_isolated(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """一個 source discover 拋例外 → rc 仍 0、該 source 標偵測失敗、其他 source 照常。"""
    ok_src = _make_fake_source("claude-code", [Session("s1", "/x", Status.IDLE, 0.0, "—", "hook")])
    bad_src = _make_fake_source("codex", RuntimeError("sqlite gone"))
    monkeypatch.setattr("ring.sources._SOURCES", [ok_src, bad_src])

    fake_nt = _make_fake_notifier("terminal-notifier", True, True)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [fake_nt])

    fake_focuser = _make_fake_focuser("tmux")
    monkeypatch.setattr("ring.focus._FOCUSERS", [fake_focuser])

    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)

    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: None)
        rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "codex" in out
    assert "偵測失敗" in out
    assert "claude-code" in out
    assert "1" in out  # claude-code 有 1 個 session


def test_doctor_reports_hook_status(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """hook_status 回三種狀態 → output 正確對應三種文案。"""
    fake_src = _make_fake_source("hook", [])
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])

    fake_nt = _make_fake_notifier("terminal-notifier", True, True)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [fake_nt])

    fake_focuser = _make_fake_focuser("tmux")
    monkeypatch.setattr("ring.focus._FOCUSERS", [fake_focuser])

    # 狀態：claude 已安裝、codex 未用
    monkeypatch.setattr(
        "ring.hook.hook_status",
        lambda: _make_fake_hook_status(claude_installed=True, codex_applicable=False),
        raising=False,
    )

    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: None)
        rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "已安裝" in out
    assert "未使用 Codex" in out

    # 狀態：claude 未安裝、codex 有用但未裝 hook
    monkeypatch.setattr(
        "ring.hook.hook_status",
        lambda: _make_fake_hook_status(claude_installed=False, codex_applicable=True, codex_installed=False),
        raising=False,
    )

    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: None)
        rc2 = cli.main(["doctor"])

    out2 = capsys.readouterr().out
    assert rc2 == 0
    assert "未安裝" in out2
    assert "ring install-hooks" in out2


def test_doctor_reports_hook_heartbeat_stale(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_src = _make_fake_source("hook", [])
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])
    monkeypatch.setattr("ring.notify._NOTIFIERS", [_make_fake_notifier("terminal-notifier", True, True)])
    monkeypatch.setattr("ring.focus._FOCUSERS", [_make_fake_focuser("tmux")])
    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)
    monkeypatch.setattr(
        "ring.commands.doctor.discover_sessions",
        lambda: [Session("stale", "/work/app", Status.WORKING, 0.0, "→ Edit", "hook", hook_stale=True)],
    )

    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: None)
        rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "Hook 心跳偵測" in out
    assert "可能失效" in out
    assert "app" in out


def test_doctor_selected_notify_backend(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """auto 模式：有可用 notifier → output 含 auto 實際選中；backend=none → 不發通知。"""
    fake_src = _make_fake_source("hook", [])
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])

    fake_focuser = _make_fake_focuser("tmux")
    monkeypatch.setattr("ring.focus._FOCUSERS", [fake_focuser])

    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)

    # backend=auto，terminal-notifier 可用 → 選中 terminal-notifier
    tn = _make_fake_notifier("terminal-notifier", True, True)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [tn])
    monkeypatch.setattr("ring.commands.doctor.get_config", lambda: cli.Config(notify_backend="auto"))

    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: None)
        cli.main(["doctor"])

    out = capsys.readouterr().out
    # 同一行確認選中正確後端
    assert "auto 實際選中：terminal-notifier" in out

    # backend=none → 不發通知（附原因 backend=none）
    monkeypatch.setattr("ring.commands.doctor.get_config", lambda: cli.Config(notify_backend="none"))
    with monkeypatch.context() as m:
        m.setattr("shutil.which", lambda name: None)
        cli.main(["doctor"])

    out2 = capsys.readouterr().out
    assert "auto 實際選中：不發通知（backend=none）" in out2


def test_doctor_mac_notification_style_hint(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """macOS 上後端可用不等於通知框會顯示；doctor 要提示檢查系統通知樣式。"""
    fake_src = _make_fake_source("hook", [])
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])

    fake_focuser = _make_fake_focuser("tmux")
    monkeypatch.setattr("ring.focus._FOCUSERS", [fake_focuser])

    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [_make_fake_notifier("terminal-notifier", True, True)])
    monkeypatch.setattr("ring.commands.doctor.get_config", lambda: cli.Config(notify_backend="auto"))

    with monkeypatch.context() as m:
        m.setattr("ring.cli.sys.platform", "darwin")
        m.setattr("shutil.which", lambda name: None)
        rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0
    assert "若只聽到聲音但沒有通知框" in out
    assert "Banner/Alert" in out


def test_doctor_focuser_availability(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """tmux/iTerm2/Terminal 各自可用/不可用狀態正確反映。"""
    fake_src = _make_fake_source("hook", [])
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])

    fake_nt = _make_fake_notifier("terminal-notifier", True, True)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [fake_nt])

    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)

    tmux_f = _make_fake_focuser("tmux")
    iterm_f = _make_fake_focuser("iTerm2")
    terminal_f = _make_fake_focuser("Terminal")
    monkeypatch.setattr("ring.focus._FOCUSERS", [tmux_f, iterm_f, terminal_f])

    # tmux 在、osascript 在、iTerm2 跑著、Terminal 沒跑
    def fake_which(name: str) -> str | None:
        return "/usr/bin/tmux" if name == "tmux" else ("/usr/bin/osascript" if name == "osascript" else None)

    def fake_osascript(script: str) -> tuple[int, str, str]:
        if "iTerm2" in script:
            return 0, "true", ""
        return 0, "false", ""

    with (
        patch("ring.osascript.osascript", fake_osascript),
        patch("shutil.which", fake_which),
    ):
        rc = cli.main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 0

    # 逐行精確斷言：tmux 可用、iTerm2 可用、Terminal 不可用
    lines = out.splitlines()
    tmux_line = next(line for line in lines if "tmux" in line and "iTerm2" not in line)
    iterm_line = next(line for line in lines if "iTerm2" in line)
    terminal_line = next(line for line in lines if "Terminal" in line and "iTerm2" not in line)
    assert "可用" in tmux_line and "不可用" not in tmux_line
    assert "可用" in iterm_line and "不可用" not in iterm_line
    assert "不可用" in terminal_line


def test_doctor_help_does_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    """ring doctor --help → rc=0，印 usage，不呼叫診斷主體。"""
    with patch.object(cli, "run_doctor") as mock_doctor:
        rc = cli.main(["doctor", "--help"])
    assert rc == 0
    mock_doctor.assert_not_called()
    out = capsys.readouterr().out
    assert "usage: ring doctor" in out


def test_doctor_is_read_only(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """doctor 全程不呼叫任何寫入入口（install_hooks / notify_waiting / focus.jump）。"""
    fake_src = _make_fake_source("hook", [])
    monkeypatch.setattr("ring.sources._SOURCES", [fake_src])

    fake_nt = _make_fake_notifier("terminal-notifier", True, True)
    monkeypatch.setattr("ring.notify._NOTIFIERS", [fake_nt])

    fake_focuser = _make_fake_focuser("tmux")
    monkeypatch.setattr("ring.focus._FOCUSERS", [fake_focuser])

    monkeypatch.setattr("ring.hook.hook_status", lambda: _make_fake_hook_status(), raising=False)

    with (
        patch("ring.hook.install_hooks") as mock_install,
        patch("ring.hook.uninstall_hooks") as mock_uninstall,
        patch("ring.notify.notify_waiting") as mock_notify,
        patch("ring.focus.jump") as mock_jump,
        patch("shutil.which", return_value=None),
    ):
        rc = cli.main(["doctor"])

    assert rc == 0
    mock_install.assert_not_called()
    mock_uninstall.assert_not_called()
    mock_notify.assert_not_called()
    mock_jump.assert_not_called()


def test_doctor_unknown_arg_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    """ring doctor --unknown-flag → rc=2（args 錯誤）。"""
    rc = cli.main(["doctor", "--unknown"])
    assert rc == 2


# ---------------------------------------------------------------------------
# gc 子命令
# ---------------------------------------------------------------------------


def test_gc_help_does_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    with patch.object(cli, "run_gc") as mock_gc:
        rc = cli.main(["gc", "--help"])
    assert rc == 0
    mock_gc.assert_not_called()
    assert "usage: ring gc" in capsys.readouterr().out


def test_gc_routes_to_gc_module(capsys: pytest.CaptureFixture[str]) -> None:
    from ring.gc import GcResult

    result = GcResult(candidates=[], deleted=[], errors=[], dry_run=True)
    with patch("ring.commands.gc.gc_run", return_value=result) as mock_gc:
        rc = cli.main(["gc", "--dry-run", "--older-than", "1d"])

    assert rc == 0
    mock_gc.assert_called_once()
    kwargs = mock_gc.call_args.kwargs
    assert kwargs["dry_run"] is True
    assert kwargs["older_than"] == 86400
    assert "RiNG GC" in capsys.readouterr().out


def test_gc_bad_duration_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["gc", "--older-than", "nope"])
    assert rc == 2
    assert "nope" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --format json / oneline（機器可讀輸出）
# ---------------------------------------------------------------------------


def _format_sessions() -> list[Session]:
    return [
        Session(
            "w",
            "/x/maigo",
            Status.WAITING,
            0.0,
            "→ 等你確認權限",
            "hook",
            provider="claude-code",
            waiting_kind="permission",
            heartbeat_at=123.0,
            hook_stale=True,
        ),
        Session("a", "/x/maigo", Status.WORKING, 0.0, "→ Edit", "scan", provider="claude-code", todo=(2, 5)),
        Session("b", "/y/blog", Status.IDLE, 0.0, "—", "codex", provider="codex"),
    ]


def test_format_json_outputs_machine_readable_board(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json

    monkeypatch.setattr(cli, "board", lambda show_all: _format_sessions())
    monkeypatch.setattr(cli, "running_agent_pids", lambda: [1, 2])
    monkeypatch.setattr(cli, "load_labels", lambda: {"w": "重構登入"})
    rc = cli.main(["--format", "json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["agent_processes"] == 2
    assert data["counts"] == {"waiting": 1, "working": 1, "idle": 1, "ended": 0}
    by_id = {s["session_id"]: s for s in data["sessions"]}
    assert by_id["w"]["label"] == "重構登入"
    assert by_id["w"]["status"] == "waiting"
    assert by_id["w"]["marker"] == "🔴"
    assert by_id["w"]["waiting_kind"] == "permission"
    assert by_id["w"]["waiting_icon"] == "🔐"
    assert by_id["w"]["heartbeat_at"] == 123.0
    assert by_id["w"]["hook_stale"] is True
    assert by_id["a"]["todo"] == {"done": 2, "total": 5}
    assert by_id["b"]["todo"] is None
    assert by_id["a"]["project"] == "maigo"


def test_format_oneline_counts_nonzero_statuses(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: _format_sessions())
    rc = cli.main(["--format", "oneline"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == "🔴1 🟢1 🟡1"


def test_format_oneline_empty_board_prints_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: [])
    rc = cli.main(["--format", "oneline"])
    assert rc == 0
    assert capsys.readouterr().out.strip() == ""


def test_format_rejects_watch(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["--watch", "--format", "json"])
    assert rc == 2
    assert "--format" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# completion 子命令
# ---------------------------------------------------------------------------


def test_completion_zsh_script(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["completion", "zsh"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "compdef _ring ring" in out
    assert "install-hooks" in out
    assert "digest:away summary" in out
    assert "session id or unique prefix" in out
    assert "notify_backend" in out  # config 鍵動態帶入


def test_completion_bash_script(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["completion", "bash"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "complete -F _ring_completion ring" in out
    assert "--format" in out
    assert "digest) COMPREPLY" in out
    assert "focus) COMPREPLY=()" in out
    assert "notify_backend" in out


def test_completion_requires_shell(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["completion"]) == 2
    assert "zsh|bash" in capsys.readouterr().err


def test_completion_unknown_shell(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["completion", "fish"]) == 2
    assert "fish" in capsys.readouterr().err


def test_completion_help_does_not_print_script(capsys: pytest.CaptureFixture[str]) -> None:
    rc = cli.main(["completion", "--help"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "usage: ring completion" in out
    assert "compdef" not in out


# ---------------------------------------------------------------------------
# digest 子命令
# ---------------------------------------------------------------------------


def test_digest_prints_mixed_summary(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import time

    from ring.stats import WaitSpan

    now = time.time()
    sessions = [
        Session(
            "w",
            "/x/maigo",
            Status.WAITING,
            now - 600,
            "→ Permission",
            "hook",
            waiting_kind="permission",
            waiting_detail="Bash: npm test",
        ),
        Session("i", "/x/blog", Status.IDLE, now - 500, "—", "hook"),
        Session("e", "/x/old", Status.ENDED, now - 400, "—", "hook"),
    ]
    monkeypatch.setattr("ring.commands.digest.discover_sessions", lambda: sessions)
    monkeypatch.setattr(
        "ring.commands.digest.collect_waits",
        lambda since, now=None: [WaitSpan("w", "maigo", 100.0, 30.0, True)],
    )

    rc = cli.main(["digest", "--since", "1h"])
    out = capsys.readouterr().out

    assert rc == 0
    assert "RiNG digest" in out
    assert "正在等你" in out
    assert "Bash: npm test" in out
    assert "已停著" in out
    assert "已離場" in out
    assert "等待統計" in out


def test_digest_json_schema(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import json

    sessions = [Session("w", "/x/maigo", Status.WAITING, 100.0, "→ Permission", "hook")]
    monkeypatch.setattr("ring.commands.digest.discover_sessions", lambda: sessions)
    monkeypatch.setattr("ring.commands.digest.collect_waits", lambda since, now=None: [])

    rc = cli.main(["digest", "--format", "json"])
    data = json.loads(capsys.readouterr().out)

    assert rc == 0
    assert data["since"] == "4h"
    assert data["waiting"][0]["session_id"] == "w"
    assert data["idle"] == []
    assert data["ended"] == []
    assert data["waits"] == {"count": 0, "total_seconds": 0, "ongoing": 0}


def test_digest_empty_state(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr("ring.commands.digest.discover_sessions", lambda: [])
    monkeypatch.setattr("ring.commands.digest.collect_waits", lambda since, now=None: [])

    assert cli.main(["digest"]) == 0
    assert "沒有 session 活動" in capsys.readouterr().out


def test_digest_bad_since_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["digest", "--since", "bad"]) == 2
    assert "bad" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# stats 子命令
# ---------------------------------------------------------------------------


def test_stats_no_data_hints_hooks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("ring.stats.EVENTS_PATH", tmp_path / "events.jsonl")
    rc = cli.main(["stats", "--lang", "en"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "install-hooks" in out


def test_stats_prints_project_table(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import time as _time

    from ring.stats import log_transition

    events = tmp_path / "events.jsonl"
    monkeypatch.setattr("ring.stats.EVENTS_PATH", events)
    now = _time.time()
    log_transition("s1", "claude-code", "/x/maigo", "waiting", path=events, now=now - 100)
    log_transition("s1", "claude-code", "/x/maigo", "working", path=events, now=now - 55)

    rc = cli.main(["stats"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "maigo" in out
    assert "45s" in out
    assert "全部" in out


def test_stats_bad_since_returns_two(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["stats", "--since", "nope"]) == 2
    assert "nope" in capsys.readouterr().err


def test_stats_help_does_not_run(capsys: pytest.CaptureFixture[str]) -> None:
    with patch("ring.commands.stats.collect_waits") as mock_collect:
        rc = cli.main(["stats", "--help"])
    assert rc == 0
    mock_collect.assert_not_called()
    assert "usage: ring stats" in capsys.readouterr().out
