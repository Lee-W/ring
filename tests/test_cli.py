from unittest.mock import patch

import pytest

import ring.cli as cli
from ring.registry import Session, Status


def _sessions() -> list[Session]:
    return [Session("a", "/x/maigo", Status.WORKING, 0.0, "→ Edit", "scan")]


def test_main_snapshot_en(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: _sessions())
    monkeypatch.setattr(cli, "running_claude_pids", lambda: [1])
    rc = cli.main(["--lang", "en", "--no-legend"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "on stage" in out
    assert "maigo" in out


def test_main_snapshot_default_is_zh(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: _sessions())
    monkeypatch.setattr(cli, "running_claude_pids", lambda: [1])
    rc = cli.main(["--no-legend"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "在場" in out  # 預設台灣漢語


def test_main_empty_board(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(cli, "board", lambda show_all: [])
    monkeypatch.setattr(cli, "running_claude_pids", lambda: [])
    assert cli.main(["--lang", "en"]) == 0
    assert "stage" in capsys.readouterr().out


def test_peek_lang() -> None:
    assert cli._peek_lang(["--lang", "en"]) == "en"
    assert cli._peek_lang(["--lang=zh-Hant"]) == "zh-Hant"
    assert cli._peek_lang(["--watch"]) is None


def test_version_exits() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--version"])


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
# focus <session_id> 路由
# ---------------------------------------------------------------------------


def test_focus_routes_to_jump_when_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """focus <uuid> 找到 session → 呼叫 focus.jump。"""
    session = Session("test-uuid", "/x/proj", Status.WAITING, 0.0, "→ Edit", "hook")
    jump_called: list[Session] = []

    def fake_get_by_id(sid: str) -> Session | None:
        return session if sid == "test-uuid" else None

    def fake_jump(s: Session) -> tuple[bool, str]:
        jump_called.append(s)
        return True, "jumped"

    with patch("ring.sources.get_by_id", fake_get_by_id), patch("ring.focus.jump", fake_jump):
        rc = cli.main(["focus", "test-uuid"])
    assert rc == 0
    assert jump_called == [session]


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
