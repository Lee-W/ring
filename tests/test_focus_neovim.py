"""``ring.focus.neovim`` 的 helper 層測試。

``try_focus`` 的整合路徑放在 tests/test_focus.py；這裡補的是它底下那些直接打 ``ps`` 或
procfs 的小函式——在真機上它們的輸出不可預期，因此一律把 ``_run`` 換成假的。
"""

from __future__ import annotations

import subprocess

import pytest

from ring.focus import neovim
from ring.registry import Session, Status


def _sess(tty: str | None = None) -> Session:
    return Session("a", "/x", Status.WAITING, 0.0, "-", "scan", tty=tty)


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["ps"], returncode, stdout=stdout, stderr=stderr)


def _patch_run(
    monkeypatch: pytest.MonkeyPatch,
    result: subprocess.CompletedProcess[str] | None,
    calls: list[list[str]] | None = None,
) -> None:
    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str] | None:
        if calls is not None:
            calls.append(cmd)
        return result

    monkeypatch.setattr(neovim, "_run", fake_run)


class TestRun:
    def test_returns_completed_process(self, monkeypatch: pytest.MonkeyPatch) -> None:
        done = _completed(stdout="ok")

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            return done

        monkeypatch.setattr("ring.focus.neovim.subprocess.run", fake_run)
        assert neovim._run(["ps"]) is done

    @pytest.mark.parametrize(
        "exc",
        [OSError("no ps"), subprocess.TimeoutExpired(cmd="ps", timeout=3), subprocess.SubprocessError("boom")],
    )
    def test_swallows_process_errors(self, monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
        """ps 不存在或逾時只代表這個 focuser 用不上，不該讓整個 jump 爆炸。"""

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
            raise exc

        monkeypatch.setattr("ring.focus.neovim.subprocess.run", fake_run)
        assert neovim._run(["ps"]) is None


class TestTtyPids:
    def test_parses_pids_and_strips_dev_prefix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []
        _patch_run(monkeypatch, _completed(stdout=" 101\n 202\nnot-a-pid\n"), calls)

        assert neovim._tty_pids("/dev/ttys003") == [101, 202]
        assert calls == [["ps", "-o", "pid=", "-t", "ttys003"]]

    def test_returns_empty_when_run_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, None)
        assert neovim._tty_pids("/dev/ttys003") == []

    def test_returns_empty_on_nonzero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, _completed(stdout="101", returncode=1))
        assert neovim._tty_pids("/dev/ttys003") == []


class TestProcessTable:
    def test_parses_basename_and_skips_malformed_rows(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(
            monkeypatch,
            _completed(
                stdout="\n".join(
                    [
                        "  1     0 /sbin/launchd",
                        " 42     1 /opt/homebrew/bin/nvim",
                        " 43    42 nvim-bin",
                        "too few",  # 欄位不足
                        "pid ppid comm",  # 非數字
                    ]
                )
            ),
        )

        parents, commands = neovim._process_table()

        assert parents == {1: 0, 42: 1, 43: 42}
        assert commands == {1: "launchd", 42: "nvim", 43: "nvim-bin"}

    def test_returns_empty_tables_when_run_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, None)
        assert neovim._process_table() == ({}, {})

    def test_returns_empty_tables_on_nonzero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, _completed(stdout="1 0 launchd", returncode=1))
        assert neovim._process_table() == ({}, {})


class TestAncestors:
    def test_walks_up_to_init(self) -> None:
        assert neovim._ancestors(30, {30: 20, 20: 10, 10: 1}) == [30, 20, 10]

    def test_stops_on_cycle(self) -> None:
        """ps 表理論上不該有環，但成環時必須停，不能無限迴圈。"""
        assert neovim._ancestors(5, {5: 6, 6: 5}) == [5, 6]

    def test_unknown_parent_ends_chain(self) -> None:
        assert neovim._ancestors(7, {}) == [7]


class _FakeEnvironPath:
    """只實作 ``_nvim_address`` 用得到的 ``read_bytes``；``content`` 為 None 代表讀不到。"""

    def __init__(self, content: bytes | None) -> None:
        self._content = content

    def __call__(self, path: str) -> _FakeEnvironPath:
        return self

    def read_bytes(self) -> bytes:
        if self._content is None:
            raise OSError("no procfs")
        return self._content


class TestNvimAddress:
    def test_linux_reads_procfs_environ(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ring.focus.neovim.sys.platform", "linux")
        monkeypatch.setattr(neovim, "Path", _FakeEnvironPath(b"PATH=/usr/bin\x00NVIM=/run/nvim.sock\x00"))
        _patch_run(monkeypatch, None)

        assert neovim._nvim_address(42) == "/run/nvim.sock"

    def test_linux_falls_back_to_ps_when_procfs_unreadable(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ring.focus.neovim.sys.platform", "linux")
        monkeypatch.setattr(neovim, "Path", _FakeEnvironPath(None))
        _patch_run(monkeypatch, _completed(stdout="TERM=xterm NVIM=/tmp/from-ps.sock nvim"))

        assert neovim._nvim_address(42) == "/tmp/from-ps.sock"

    def test_reads_env_from_ps_output(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ring.focus.neovim.sys.platform", "darwin")
        calls: list[list[str]] = []
        _patch_run(monkeypatch, _completed(stdout="SHELL=/bin/zsh NVIM=/tmp/nvim.sock /bin/zsh"), calls)

        assert neovim._nvim_address(42) == "/tmp/nvim.sock"
        assert calls == [["ps", "eww", "-p", "42", "-o", "command="]]

    def test_returns_empty_when_env_absent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ring.focus.neovim.sys.platform", "darwin")
        _patch_run(monkeypatch, _completed(stdout="SHELL=/bin/zsh /bin/zsh"))
        assert neovim._nvim_address(42) == ""

    def test_returns_empty_when_run_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ring.focus.neovim.sys.platform", "darwin")
        _patch_run(monkeypatch, None)
        assert neovim._nvim_address(42) == ""

    def test_returns_empty_on_nonzero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("ring.focus.neovim.sys.platform", "darwin")
        _patch_run(monkeypatch, _completed(stdout="NVIM=/tmp/nvim.sock", returncode=1))
        assert neovim._nvim_address(42) == ""


class TestPidTty:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("ttys001\n", "/dev/ttys001"),
            ("/dev/ttys002\n", "/dev/ttys002"),
            ("?\n", ""),
            ("??\n", ""),
            ("\n", ""),
        ],
    )
    def test_normalizes_tty(self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: str) -> None:
        _patch_run(monkeypatch, _completed(stdout=raw))
        assert neovim._pid_tty(42) == expected

    def test_returns_empty_when_run_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, None)
        assert neovim._pid_tty(42) == ""

    def test_returns_empty_on_nonzero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, _completed(stdout="ttys001", returncode=1))
        assert neovim._pid_tty(42) == ""


class TestFindNeovim:
    def test_returns_address_and_owning_nvim_pid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(neovim, "_tty_pids", lambda _tty: [300])
        monkeypatch.setattr(neovim, "_process_table", lambda: ({300: 200, 200: 100, 100: 1}, {200: "nvim", 100: "zsh"}))
        monkeypatch.setattr(neovim, "_nvim_address", lambda _pid: "/tmp/nvim.sock")

        assert neovim._find_neovim("/dev/ttys003") == ("/tmp/nvim.sock", 200)

    def test_skips_pids_without_nvim_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen: list[int] = []

        def fake_address(pid: int) -> str:
            seen.append(pid)
            return "/tmp/nvim.sock" if pid == 301 else ""

        monkeypatch.setattr(neovim, "_tty_pids", lambda _tty: [300, 301])
        monkeypatch.setattr(neovim, "_process_table", lambda: ({301: 200, 300: 1}, {200: "nvim-bin"}))
        monkeypatch.setattr(neovim, "_nvim_address", fake_address)

        assert neovim._find_neovim("/dev/ttys003") == ("/tmp/nvim.sock", 200)
        assert seen == [300, 301]

    def test_returns_none_when_no_nvim_ancestor(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """繼承了 NVIM 但祖先鏈裡沒有 nvim（例如 nvim 已退場）——不能亂猜一個 pid。"""
        monkeypatch.setattr(neovim, "_tty_pids", lambda _tty: [300])
        monkeypatch.setattr(neovim, "_process_table", lambda: ({300: 1}, {300: "zsh"}))
        monkeypatch.setattr(neovim, "_nvim_address", lambda _pid: "/tmp/nvim.sock")

        assert neovim._find_neovim("/dev/ttys003") is None

    def test_returns_none_without_tty_pids(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(neovim, "_tty_pids", lambda _tty: [])
        monkeypatch.setattr(neovim, "_process_table", lambda: ({}, {}))
        assert neovim._find_neovim("/dev/ttys003") is None


class TestTryFocusFailureModes:
    @pytest.fixture(autouse=True)
    def _found_nvim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(neovim, "which", lambda _name: "/usr/bin/nvim")
        monkeypatch.setattr(neovim, "_find_neovim", lambda _tty: ("/tmp/nvim.sock", 42))

    def test_skips_when_nvim_missing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(neovim, "which", lambda _name: None)
        assert neovim.focuser.try_focus(_sess(tty="/dev/ttys003")) is None

    def test_skips_when_no_owning_nvim(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(neovim, "_find_neovim", lambda _tty: None)
        assert neovim.focuser.try_focus(_sess(tty="/dev/ttys003")) is None

    def test_reports_failure_when_remote_call_cannot_run(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, None)
        assert neovim.focuser.try_focus(_sess(tty="/dev/ttys003")) == (False, "nvim remote request failed")

    def test_reports_stderr_on_nonzero_returncode(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, _completed(returncode=1, stderr=" E5555: connection refused \n"))
        assert neovim.focuser.try_focus(_sess(tty="/dev/ttys003")) == (False, "E5555: connection refused")

    def test_falls_back_to_generic_message_when_stderr_empty(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_run(monkeypatch, _completed(returncode=1, stderr="  \n"))
        assert neovim.focuser.try_focus(_sess(tty="/dev/ttys003")) == (False, "nvim remote request failed")

    def test_unparsable_stdout_counts_as_no_buffer(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """remote-expr 回傳非數字（例如 nvim 印了錯誤訊息）時當成沒找到，不是當成成功。"""
        _patch_run(monkeypatch, _completed(stdout="E5108: nope"))
        assert neovim.focuser.try_focus(_sess(tty="/dev/ttys003")) == (
            False,
            "terminal buffer for /dev/ttys003 not found",
        )

    def test_keeps_original_tty_when_outer_lookup_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """外層 tty 查不到時保留原本的 tty，讓後續 focuser 至少還有東西可比對。"""
        _patch_run(monkeypatch, _completed(stdout="7\n"))
        monkeypatch.setattr(neovim, "_pid_tty", lambda _pid: "")
        session = _sess(tty="/dev/ttys003")

        assert neovim.focuser.try_focus(session) == (True, "Neovim buffer 7")
        assert session.tty == "/dev/ttys003"
