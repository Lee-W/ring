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
    monkeypatch.setattr("ring.focus.applescript.shutil.which", lambda _name: "/usr/bin/osascript")

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    monkeypatch.setattr("ring.osascript.subprocess.run", fake_run)
    assert focus.jump(_sess(tty="/dev/ttys003")) == (True, "iTerm2 /dev/ttys003")


def test_falls_through_iterm2_to_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    outs: Iterator[str] = iter(["notfound", "ok"])  # iTerm2 不歸它管 → 換 Terminal
    monkeypatch.setattr("ring.focus.applescript.shutil.which", lambda _name: "/usr/bin/osascript")

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outs), stderr="")

    monkeypatch.setattr("ring.osascript.subprocess.run", fake_run)
    assert focus.jump(_sess(tty="/dev/ttys003")) == (True, "Terminal /dev/ttys003")


def test_stale_tty_reports_closed_terminal_tab(monkeypatch: pytest.MonkeyPatch) -> None:
    outs: Iterator[str] = iter(["notfound", "notfound"])
    monkeypatch.setattr("ring.focus.applescript.shutil.which", lambda _name: "/usr/bin/osascript")
    # 在 Linux（如 CI runner）上 linux-wm focuser 也會接手；它與 osascript 共用 subprocess.run，
    # 會多吃掉 outs 一格而 StopIteration。讓 wmctrl「找不到」→ linux-wm 直接 return None，
    # 測試聚焦在 macOS 終端 app fallback、且跨平台穩定。
    monkeypatch.setattr("ring.focus.linux_wm.shutil.which", lambda _name: None)

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outs), stderr="")

    monkeypatch.setattr("ring.osascript.subprocess.run", fake_run)

    ok, msg = focus.jump(_sess(tty="/dev/ttys999"))

    assert ok is False
    assert "可能已關閉" in msg


def test_surfaces_osascript_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.focus.applescript.shutil.which", lambda _name: "/usr/bin/osascript")

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Not authorized")

    monkeypatch.setattr("ring.osascript.subprocess.run", fake_run)
    ok, msg = focus.jump(_sess(tty="/dev/ttys999"))
    assert ok is False
    assert "Not authorized" in msg


# --- Linux X11 視窗 focuser（wmctrl，best-effort fallback）---

from ring.focus import linux_wm  # noqa: E402


def _wm_run(activate_rc: int = 0, windows: str = "0x111 0 150 host title\n") -> object:
    """模擬 linux_wm 用到的各指令：ps（tty pids / ppid map）、wmctrl（列視窗 / 聚焦）。

    場景：tty 上的 shell(200) → 終端模擬器(150) → init(1)，模擬器 150 擁有視窗 0x111。
    """

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        def cp(out: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(cmd, rc, stdout=out, stderr="")

        if cmd[:2] == ["ps", "-o"]:  # tty 上的 pids
            return cp("200\n")
        if cmd[:2] == ["ps", "-eo"]:  # pid → ppid
            return cp("200 150\n150 1\n")
        if cmd[:2] == ["wmctrl", "-lp"]:  # 視窗清單
            return cp(windows)
        if cmd[:2] == ["wmctrl", "-i"]:  # 聚焦
            return cp("", activate_rc)
        return cp("", 1)

    return fake_run


def _patch_linux(monkeypatch: pytest.MonkeyPatch, run: object) -> None:
    monkeypatch.setattr("ring.focus.linux_wm.sys.platform", "linux")
    monkeypatch.setattr("ring.focus.linux_wm.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("ring.focus.linux_wm.subprocess.run", run)


def test_linux_wm_registered_in_builtin() -> None:
    assert "linux-wm" in focus._BUILTIN


def test_linux_wm_activates_owning_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_linux(monkeypatch, _wm_run())
    assert linux_wm.focuser.try_focus(_sess(tty="/dev/pts/3")) == (True, "linux-wm 0x111")


def test_linux_wm_skips_when_not_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.focus.linux_wm.sys.platform", "darwin")
    assert linux_wm.focuser.try_focus(_sess(tty="/dev/pts/3")) is None


def test_linux_wm_skips_without_wmctrl(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.focus.linux_wm.sys.platform", "linux")
    monkeypatch.setattr("ring.focus.linux_wm.shutil.which", lambda _name: None)
    assert linux_wm.focuser.try_focus(_sess(tty="/dev/pts/3")) is None


def test_linux_wm_skips_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_linux(monkeypatch, _wm_run())
    assert linux_wm.focuser.try_focus(_sess()) is None


def test_linux_wm_falls_through_when_no_owning_window(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_linux(monkeypatch, _wm_run(windows="0x999 0 4242 host other\n"))  # 視窗不屬這條祖先鏈
    assert linux_wm.focuser.try_focus(_sess(tty="/dev/pts/3")) is None


def test_linux_wm_reports_activate_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_linux(monkeypatch, _wm_run(activate_rc=1))
    ok, msg = linux_wm.focuser.try_focus(_sess(tty="/dev/pts/3"))  # type: ignore[misc]
    assert ok is False
    assert "0x111" in msg
