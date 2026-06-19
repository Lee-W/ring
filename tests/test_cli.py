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
