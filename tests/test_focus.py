import subprocess
from collections.abc import Iterator

import pytest

import ring.focus as focus
from ring.registry import Session, Status


def _sess(tmux_target: str | None = None, tty: str | None = None) -> Session:
    return Session("a", "/x", Status.WAITING, 0.0, "-", "scan", tmux_target=tmux_target, tty=tty)


def test_jump_no_target_reports_reason() -> None:
    ok, msg = focus.jump(_sess())
    assert ok is False
    assert "tty" in msg


def test_jump_ended_session_refuses_before_focusers(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session("ended", "/x", Status.ENDED, 0.0, "-", "hook", tmux_target="main:1.0", tty="/dev/ttys003")
    monkeypatch.setattr("ring.focus._FOCUSERS", [])

    ok, msg = focus.jump(session)

    assert ok is False
    assert "已離場" in msg


def test_tmux_focuser(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr("ring.focus.tmux.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("ring.focus.tmux.subprocess.run", fake_run)
    assert focus.jump(_sess(tmux_target="main:1.0")) == (True, "tmux main:1.0")
    assert ["tmux", "switch-client", "-t", "main"] in calls


def test_iterm2_focuser_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.focus.base.shutil.which", lambda _name: "/usr/bin/osascript")

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("ring.focus.base.subprocess.run", fake_run)
    assert focus.jump(_sess(tty="/dev/ttys003")) == (True, "iTerm2 /dev/ttys003")


def test_falls_through_iterm2_to_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    outs: Iterator[str] = iter(["notfound", "ok"])  # iTerm2 不歸它管 → 換 Terminal
    monkeypatch.setattr("ring.focus.base.shutil.which", lambda _name: "/usr/bin/osascript")

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outs), stderr="")

    monkeypatch.setattr("ring.focus.base.subprocess.run", fake_run)
    assert focus.jump(_sess(tty="/dev/ttys003")) == (True, "Terminal /dev/ttys003")


def test_surfaces_osascript_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.focus.base.shutil.which", lambda _name: "/usr/bin/osascript")

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Not authorized")

    monkeypatch.setattr("ring.focus.base.subprocess.run", fake_run)
    ok, msg = focus.jump(_sess(tty="/dev/ttys999"))
    assert ok is False
    assert "Not authorized" in msg
