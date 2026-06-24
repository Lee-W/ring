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


def test_config_shows_path_and_settings(capsys: pytest.CaptureFixture[str]) -> None:
    """ring config 印出設定檔路徑與目前生效的設定欄位。"""
    rc = cli.main(["config", "--lang", "en"])
    out = capsys.readouterr().out
    assert rc == 0
    assert str(cli.CONFIG_PATH) in out
    assert "notify_backend" in out  # dataclass 欄位都列出來
    assert "Effective settings" in out


def test_config_marks_overridden_values(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
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


def test_focus_silent_when_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """focus <uuid> 查不到 → 安靜回 0，不拋例外。"""
    jump_called: list[Session] = []

    def fake_get_by_id(sid: str) -> Session | None:
        return None

    def fake_jump(s: Session) -> tuple[bool, str]:
        jump_called.append(s)
        return True, "jumped"

    with patch("ring.sources.get_by_id", fake_get_by_id), patch("ring.focus.jump", fake_jump):
        rc = cli.main(["focus", "nonexistent-uuid"])
    assert rc == 0
    assert jump_called == [], "查不到時不應呼叫 jump"
