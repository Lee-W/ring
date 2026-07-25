import json
import shutil
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

import ring.focus as focus
from ring.focus import neovim
from ring.registry import Session, Status


def _sess(tmux_target: str | None = None, tty: str | None = None) -> Session:
    return Session("a", "/x", Status.WAITING, 0.0, "-", "scan", tmux_target=tmux_target, tty=tty)


@pytest.fixture(autouse=True)
def _disable_host_neovim(monkeypatch: pytest.MonkeyPatch) -> None:
    """既有 focuser tests 不應取決於執行測試的機器是否裝了 nvim。"""
    monkeypatch.setattr("ring.focus.neovim.which", lambda _name: None)


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


# --- Neovim :terminal focuser ---


def test_neovim_registered_before_outer_focusers() -> None:
    assert list(focus._BUILTIN).index("Neovim") < list(focus._BUILTIN).index("tmux")


def test_neovim_skips_without_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.focus.neovim.which", lambda _name: "/usr/bin/nvim")
    assert neovim.focuser.try_focus(_sess()) is None


def test_neovim_switches_buffer_and_retargets_outer_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    session = _sess(tty="/dev/ttys009")
    monkeypatch.setattr("ring.focus.neovim.which", lambda _name: "/usr/bin/nvim")
    monkeypatch.setattr("ring.focus.neovim._find_neovim", lambda _tty: ("/tmp/nvim.sock", 42))
    monkeypatch.setattr("ring.focus.neovim._pid_tty", lambda _pid: "/dev/ttys001")

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="7\n", stderr="")

    monkeypatch.setattr("ring.focus.neovim._run", fake_run)

    assert neovim.focuser.try_focus(session) == (True, "Neovim buffer 7")
    assert session.tty == "/dev/ttys001"
    assert calls[0][:4] == ["/usr/bin/nvim", "--server", "/tmp/nvim.sock", "--remote-expr"]
    assert "/dev/ttys009" in calls[0][4]


def test_neovim_reports_missing_terminal_buffer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.focus.neovim.which", lambda _name: "/usr/bin/nvim")
    monkeypatch.setattr("ring.focus.neovim._find_neovim", lambda _tty: ("/tmp/nvim.sock", 42))
    monkeypatch.setattr(
        "ring.focus.neovim._run",
        lambda cmd: subprocess.CompletedProcess(cmd, 0, stdout="0\n", stderr=""),
    )
    assert neovim.focuser.try_focus(_sess(tty="/dev/ttys009")) == (
        False,
        "terminal buffer for /dev/ttys009 not found",
    )


def test_jump_continues_from_neovim_to_outer_focuser(monkeypatch: pytest.MonkeyPatch) -> None:
    class Inner:
        name = "Neovim"
        continue_after_success = True

        def try_focus(self, session: Session) -> tuple[bool, str]:
            session.tty = "/dev/ttys001"
            return True, "Neovim buffer 7"

    class Outer:
        name = "outer"

        def try_focus(self, session: Session) -> tuple[bool, str]:
            assert session.tty == "/dev/ttys001"
            return True, "outer terminal"

    monkeypatch.setattr("ring.focus._FOCUSERS", [Inner(), Outer()])
    assert focus.jump(_sess(tty="/dev/ttys009")) == (True, "Neovim buffer 7; outer terminal")


def test_jump_reports_outer_failure_after_neovim_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    class Inner:
        name = "Neovim"
        continue_after_success = True

        def try_focus(self, _session: Session) -> tuple[bool, str]:
            return True, "Neovim buffer 7"

    class Outer:
        name = "outer"

        def try_focus(self, _session: Session) -> tuple[bool, str]:
            return False, "activation denied"

    monkeypatch.setattr("ring.focus._FOCUSERS", [Inner(), Outer()])
    assert focus.jump(_sess(tty="/dev/ttys009")) == (
        False,
        "Neovim buffer 7; outer: activation denied",
    )


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


def test_iterm2_script_avoids_positional_specifiers() -> None:
    """迴歸柵欄：``repeat with w in windows`` 的迴圈變數是「按位置」參照。

    windows 順序在掃描途中被動到（剛跳過一次、或使用者點了別的視窗）就會噴
    ``Invalid index (-1719)``，跨視窗跳轉整個失敗——實測連續來回跳，舊寫法 3/3 都掛。
    ``set index of … to 1`` 會主動重排 windows，等於自己製造同一個雷。
    """
    from ring.focus.iterm2 import _SCRIPT

    assert "repeat with w in windows" not in _SCRIPT
    assert "set index of" not in _SCRIPT
    assert "on error" in _SCRIPT  # 順序真的被動到時要重掃，不是直接失敗


def test_iterm2_unraised_reports_failure_instead_of_fake_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """腳本找到分頁但驗不到視窗浮到前景時，要照實回失敗——不能顯示「跳過去了」卻沒動。"""
    # applescript 與 linux_wm 共用同一個 shutil 模組物件，分開 patch 會互相覆蓋，
    # 所以用認名字的假 which：只有 wmctrl 查不到（Linux CI 上 linux-wm 才不會插隊）。
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "wmctrl" else "/usr/bin/osascript")

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="unraised", stderr="")

    monkeypatch.setattr("ring.osascript.subprocess.run", fake_run)

    ok, msg = focus.jump(_sess(tty="/dev/ttys003"))

    assert ok is False
    assert "iTerm2" in msg
    assert "沒浮到前景" in msg


def test_jump_writes_trace_line(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """每次跳轉都要留下可歸因的證據：逐個 focuser 的回覆、最終結果。"""
    log = tmp_path / "focus.jsonl"
    monkeypatch.setattr("ring.focus.trace.FOCUS_LOG_PATH", log)
    monkeypatch.setattr("ring.focus.applescript.shutil.which", lambda _name: "/usr/bin/osascript")
    outs: Iterator[str] = iter(["notfound", "ok"])  # iTerm2 skip → Terminal 接手

    def fake_run(cmd: list[str], **kw: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout=next(outs), stderr="")

    monkeypatch.setattr("ring.osascript.subprocess.run", fake_run)

    focus.jump(_sess(tty="/dev/ttys003"))

    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["ok"] is True
    assert entry["tty"] == "/dev/ttys003"
    assert {"focuser": "iTerm2", "result": "skip"} in entry["attempts"]
    assert {"focuser": "Terminal", "result": "ok"} in entry["attempts"]


def test_trace_records_why_nothing_caught(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """全部 focuser 都 skip 而 session 有 tty＝tty 已失效，log 要看得出來。"""
    log = tmp_path / "focus.jsonl"
    monkeypatch.setattr("ring.focus.trace.FOCUS_LOG_PATH", log)
    monkeypatch.setattr("ring.focus._FOCUSERS", [])

    focus.jump(_sess(tty="/dev/ttys999"))

    entry = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
    assert entry["ok"] is False
    assert entry["attempts"] == []
    assert "可能已關閉" in entry["msg"]


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


# --- Neovim remote-expr 對真實 nvim 的 regression（沒裝 nvim 就 skip） ---

_NVIM_BIN = shutil.which("nvim")


@pytest.mark.skipif(_NVIM_BIN is None, reason="needs nvim on PATH")
def test_remote_expr_skips_dead_terminal_buffer() -> None:
    """regression: 已死的 :terminal buffer 排在活 buffer 前面時，expression 不該被
    jobpid 的 E900 炸掉，要跳過它、找到後面活著的 terminal。"""
    assert _NVIM_BIN is not None
    # 不用 pytest 的 tmp_path：unix socket 路徑上限約 104 bytes，pytest 路徑太長會 bind 失敗
    sock_dir = tempfile.mkdtemp(prefix="ring-nvim-")
    sock = str(Path(sock_dir) / "nvim.sock")
    server = subprocess.Popen(
        [_NVIM_BIN, "--clean", "--headless", "--listen", sock],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(50):
            if Path(sock).exists():
                break
            time.sleep(0.1)
        else:
            pytest.fail("nvim server socket never appeared")

        def remote(expr: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [_NVIM_BIN, "--server", sock, "--remote-expr", expr],
                capture_output=True,
                text=True,
                timeout=10,
            )

        # buffer 1：馬上結束的 terminal（等它死透，channel 關閉）
        dead_job = remote("luaeval(\"vim.fn.termopen('true')\")").stdout.strip()
        remote(f'luaeval("vim.fn.jobwait({{{dead_job}}}, 5000)[1]")')
        # buffer 2：活著的 terminal
        live_job = remote("luaeval(\"(function() vim.cmd('enew') return vim.fn.termopen('sleep 30') end)()\")")
        live_id = live_job.stdout.strip()
        # 活 terminal 的 tty（從 server 端的 jobpid → ps 反查）
        tty_lua = (
            f"(function() local ok, p = pcall(vim.fn.jobpid, {live_id}) if not ok then return '' end "
            "return vim.trim(vim.fn.system({'ps', '-o', 'tty=', '-p', tostring(p)})) end)()"
        )
        tty = remote(f'luaeval("{tty_lua}")').stdout.strip()
        assert tty, "could not resolve live terminal tty"
        tty = tty if tty.startswith("/dev/") else f"/dev/{tty}"

        result = remote(neovim._remote_expr(tty))
        assert result.returncode == 0, f"remote-expr errored: {result.stderr.strip()[-200:]}"
        assert int(result.stdout.strip()) > 0  # 找到活的 terminal buffer
    finally:
        server.kill()
        server.wait(timeout=5)
        shutil.rmtree(sock_dir, ignore_errors=True)
