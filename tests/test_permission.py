"""permission 模組：對話框解析（餵 PoC 真實畫面）與送鍵流程（mock tmux / osascript）。

fixtures 放 ``tests/fixtures/permission/``：

tmux（PoC：claude 2.1.206 + tmux 3.7b，用 ``tmux capture-pane -p`` 抓下來）：

- ``dialog-bash.txt``：一般 Bash 權限對話框（3 個選項）
- ``dialog-subagent.txt``：背景 subagent 的對話框（標題帶 "from the general-purpose agent"）
- ``no-dialog-misfire.txt``：對話框不在時誤送「2」、數字落進聊天輸入框的樣子
- ``no-dialog-after-reply.txt``：回覆成功後對話框消失、模型繼續跑的畫面
- ``dialog-wrapped-option.txt``：**手工模擬折行，非真實截圖**——由 ``dialog-bash.txt`` 手改，
  option 2 的文字撐長折成兩行（縮排 6 格），驗證「選項一折行就整份讀不到」的修法

iTerm2（PoC：同一版 claude，直接開在 iTerm2 分頁、沒有 tmux，用 ``contents of session``
抓下來）：

- ``iterm-dialog.txt``：權限對話框畫面
- ``iterm-after-reply.txt``：回覆成功後對話框消失、模型繼續跑的畫面
- ``iterm-misfire.txt``：對話框不在時誤送「2」、數字落進聊天輸入框的樣子
"""

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ring.permission as permission
from ring.focus import kitty as focus_kitty
from ring.permission import (
    ITermBackend,
    KittyBackend,
    PermissionDialog,
    ReplyOutcome,
    TmuxBackend,
    digit_in_input_line,
    parse_permission_dialog,
    select_backend,
    send_permission_reply,
)
from ring.registry import Session, Status

_FIXTURES = Path(__file__).parent / "fixtures" / "permission"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture(autouse=True)
def _no_kitty_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """預設「沒有 kitty」：本機常駐的真實 kitty socket 不該讓這個檔的測試依機器狀態飄。

    kitty 專屬測試（見下方 kitty 區段）自己 override `permission.kitty_resolve_window`。
    """
    monkeypatch.setattr(permission, "kitty_resolve_window", lambda tty: None)


def _wire_kitty(monkeypatch: pytest.MonkeyPatch, results: list[tuple[int, str]]) -> list[list[str]]:
    """把 kitty 的 subprocess 換成腳本：results 依序回放 (returncode, stdout)，記錄每次 argv。"""
    calls: list[list[str]] = []

    class _Result:
        def __init__(self, returncode: int, stdout: str) -> None:
            self.returncode = returncode
            self.stdout = stdout

    def fake_run(cmd: list[str], **kwargs: object) -> _Result:
        calls.append(cmd)
        rc, out = results.pop(0) if results else (1, "")
        return _Result(rc, out)

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/kitty")
    monkeypatch.setattr(subprocess, "run", fake_run)
    return calls


# ---------------------------------------------------------------------------
# 解析：真實畫面
# ---------------------------------------------------------------------------


def test_parse_bash_permission_dialog() -> None:
    dialog = parse_permission_dialog(_fixture("dialog-bash.txt"))
    assert dialog is not None
    assert [n for n, _text in dialog.options] == [1, 2, 3]
    assert dialog.options[0][1] == "Yes"
    assert dialog.options[1][1] == "Yes, and always allow access to poc-tmux-reply/ from this project"
    assert dialog.options[2][1] == "No"
    assert dialog.question == "Do you want to proceed?"
    assert dialog.title == "Bash command"
    assert dialog.agent == ""


def test_parse_subagent_permission_dialog() -> None:
    dialog = parse_permission_dialog(_fixture("dialog-subagent.txt"))
    assert dialog is not None
    assert len(dialog.options) == 3
    assert dialog.options[0] == (1, "Yes")
    assert dialog.options[2][1] == "No"
    assert dialog.title == "Bash command · from the general-purpose agent"
    assert dialog.agent == "general-purpose"


@pytest.mark.parametrize("name", ["no-dialog-misfire.txt", "no-dialog-after-reply.txt"])
def test_parse_no_dialog_screens(name: str) -> None:
    """沒有對話框的畫面（含誤送後、回覆成功後）→ 判定不可送。"""
    assert parse_permission_dialog(_fixture(name)) is None


def test_parse_requires_all_markers() -> None:
    """標記不齊全一律回 None：缺 footer、缺游標、編號不連續、缺問句。"""
    base = _fixture("dialog-bash.txt")
    assert parse_permission_dialog(base.replace("Esc to cancel", "")) is None
    assert parse_permission_dialog(base.replace("❯ 1.", "  1.")) is None  # 沒游標
    assert parse_permission_dialog(base.replace(" 3. No", " 4. No")) is None  # 編號跳號
    assert parse_permission_dialog(base.replace("Do you want to proceed?", "")) is None
    assert parse_permission_dialog("") is None


def test_digit_in_input_line() -> None:
    misfire = _fixture("no-dialog-misfire.txt")
    assert digit_in_input_line(misfire, "2")  # 「❯ 2」＝數字落進輸入框
    assert not digit_in_input_line(misfire, "3")
    # 對話框在場時「❯ 1. Yes」是游標選項，不是輸入框誤送。
    assert not digit_in_input_line(_fixture("dialog-bash.txt"), "1")


# ---------------------------------------------------------------------------
# 折行：選項文字跨行時仍要能正確讀出（且放寬判準不能誤吃別的結構列）
# ---------------------------------------------------------------------------

_WRAP_MARKER = "   2. Yes, and always allow access to poc-tmux-reply/ from this project\n"


def test_parse_wrapped_option_middle() -> None:
    """正向 A：中間選項（option 2）折行，續行縮排 6 格（對齊選項文字起點）。"""
    dialog = parse_permission_dialog(_fixture("dialog-wrapped-option.txt"))
    assert dialog is not None
    assert [n for n, _text in dialog.options] == [1, 2, 3]
    assert dialog.options[1][1] == "Yes, and always allow access to poc-tmux-reply/ from this project"
    assert dialog.question == "Do you want to proceed?"
    assert dialog.title == "Bash command"


def test_parse_wrapped_option_last() -> None:
    """正向 B：最後一個選項（option 3，footer 正上方）折行，仍要收集齊 3 個選項。"""
    base = _fixture("dialog-bash.txt")
    old = "   3. No\n"
    new = "   3. No, actually let me think about this more carefully before\n      deciding\n"
    assert old in base
    dialog = parse_permission_dialog(base.replace(old, new))
    assert dialog is not None
    assert len(dialog.options) == 3
    assert dialog.options[2][1] == "No, actually let me think about this more carefully before deciding"


def test_parse_wrapped_option_2space_indent_matches_6space() -> None:
    """正向 C：續行縮排格數不設限——2 格（模仿 dialog-bash.txt:17-18 問句折行樣式）
    跟 A 用的 6 格結果逐字相同。"""
    fixture_a = parse_permission_dialog(_fixture("dialog-wrapped-option.txt"))
    base = _fixture("dialog-bash.txt")
    two_space = "   2. Yes, and always allow access to poc-tmux-reply/ from this\n  project\n"
    assert _WRAP_MARKER in base
    dialog_2 = parse_permission_dialog(base.replace(_WRAP_MARKER, two_space))
    assert dialog_2 == fixture_a


@pytest.mark.parametrize(
    "poison",
    [
        "   Esc to cancel · Tab to amend",
        "   " + "─" * 12,
        "   ❯ not an option",
        "   Is this ok?",
        "Not indented",
    ],
    ids=["footer-shape", "separator", "cursor-input-line", "question-shape", "not-indented"],
)
def test_parse_continuation_poison_lines_break_collection(poison: str) -> None:
    """負向（比正向更重要）：五種「長得像續行、其實是別的結構列」的東西插進選項之間，
    都必須讓收集當場中斷 → 選項不足 2 → 回 None。"""
    base = _fixture("dialog-bash.txt")
    assert _WRAP_MARKER in base
    screen = base.replace(_WRAP_MARKER, _WRAP_MARKER + poison + "\n")
    assert parse_permission_dialog(screen) is None


def test_parse_continuation_benign_line_is_absorbed() -> None:
    """負向對照：不長得像任何結構列的縮排文字，會被當成良性續行併入前一個選項——
    證明排除規則是「有選擇性」的，不是把所有東西都擋掉。"""
    base = _fixture("dialog-bash.txt")
    assert _WRAP_MARKER in base
    screen = base.replace(_WRAP_MARKER, _WRAP_MARKER + "   plus more words\n")
    dialog = parse_permission_dialog(screen)
    assert dialog is not None
    assert len(dialog.options) == 3
    assert dialog.options[1][1].endswith("plus more words")


def test_parse_ten_plus_options() -> None:
    """11 選項畫面（程式內組出來）：全部讀出、編號連續、options[9] 是 (10, ...)。"""
    lines = [
        "",
        " Do you want to proceed?",
        " ❯ 1. Yes",
        *(f"   {n}. Option {n}" for n in range(2, 12)),
        "",
        " Esc to cancel · Tab to amend · ctrl+e to explain",
    ]
    screen = "\n".join(lines) + "\n"
    dialog = parse_permission_dialog(screen)
    assert dialog is not None
    assert [n for n, _text in dialog.options] == list(range(1, 12))
    assert dialog.options[9] == (10, "Option 10")


def test_parse_three_digit_number_not_treated_as_option() -> None:
    """`123. foo` 不是選項——`_OPTION_RE` 限 1–2 位，三位數編號列讓收集中斷回 None，
    不會被安靜地讀成編號 123 的選項。"""
    screen = (
        "\n".join(
            [
                "",
                " Do you want to proceed?",
                " ❯ 1. Yes",
                "   2. No",
                "   123. foo",
                "",
                " Esc to cancel · Tab to amend · ctrl+e to explain",
            ]
        )
        + "\n"
    )
    assert parse_permission_dialog(screen) is None


# ---------------------------------------------------------------------------
# 送鍵流程：mock capture / send（不碰真 tmux）
# ---------------------------------------------------------------------------


def _dialog() -> PermissionDialog:
    dialog = parse_permission_dialog(_fixture("dialog-bash.txt"))
    assert dialog is not None
    return dialog


def _wire(monkeypatch: pytest.MonkeyPatch, captures: list[str | None]) -> tuple[list[str], list[tuple[str, str]]]:
    """把 capture_pane / send_key 換成腳本：captures 依序回放，send 全記錄。"""
    seen: list[str] = []
    sent: list[tuple[str, str]] = []

    def fake_capture(target: str) -> str | None:
        seen.append(target)
        return captures.pop(0) if captures else None

    def fake_send(target: str, key: str) -> bool:
        sent.append((target, key))
        return True

    monkeypatch.setattr(permission, "capture_pane", fake_capture)
    monkeypatch.setattr(permission, "send_key", fake_send)
    return seen, sent


def test_reply_ok_when_dialog_disappears(monkeypatch: pytest.MonkeyPatch) -> None:
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-bash.txt"), _fixture("no-dialog-after-reply.txt")])
    outcome = send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0)
    assert outcome is ReplyOutcome.OK
    assert sent == [("main:1.0", "1")]  # 單一數字、無 Enter


def test_reply_polls_and_finishes_before_full_delay(monkeypatch: pytest.MonkeyPatch) -> None:
    """第一眼仍在時短輪詢；下一眼消失就提早完成，不固定卡滿 0.4 秒。"""
    same = _fixture("dialog-bash.txt")
    _seen, sent = _wire(monkeypatch, [same, same, _fixture("no-dialog-after-reply.txt")])
    now = [0.0]
    sleeps: list[float] = []
    # getattr 是為了繞過 mypy strict 的 implicit-reexport（permission 沒有明確 re-export time），
    # 不是安全性考量——B009 在這裡不適用。
    permission_time = getattr(permission, "time")  # noqa: B009
    monkeypatch.setattr(permission_time, "monotonic", lambda: now[0])

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(permission_time, "sleep", fake_sleep)

    outcome = send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0.4)

    assert outcome is ReplyOutcome.OK
    assert sleeps == [permission._VERIFY_POLL_INTERVAL]
    assert sum(sleeps) < 0.4
    assert sent == [("main:1.0", "1")]


def test_reply_refuses_when_no_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """二次 capture 抓不到對話框 → 不送鍵。"""
    _seen, sent = _wire(monkeypatch, [_fixture("no-dialog-after-reply.txt")])
    assert send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0) is ReplyOutcome.NO_DIALOG
    assert sent == []


def test_reply_refuses_when_capture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _seen, sent = _wire(monkeypatch, [None])
    assert send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0) is ReplyOutcome.NO_DIALOG
    assert sent == []


def test_reply_refuses_when_dialog_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """二次 capture 的對話框內容變了（換成 subagent 的請求）→ 不送鍵。"""
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-subagent.txt")])
    assert send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0) is ReplyOutcome.CHANGED
    assert sent == []


def test_reply_refuses_number_outside_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen, sent = _wire(monkeypatch, [])
    assert send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 7, delay=0) is ReplyOutcome.CHANGED
    assert seen == [] and sent == []


def test_reply_misfire_sends_backspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """送鍵瞬間對話框消失、數字落進輸入框（❯ 2）→ 補 Backspace。"""
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-bash.txt"), _fixture("no-dialog-misfire.txt")])
    outcome = send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 2, delay=0)
    assert outcome is ReplyOutcome.MISFIRE
    assert sent == [("main:1.0", "2"), ("main:1.0", "BSpace")]


def test_reply_warns_when_dialog_still_present(monkeypatch: pytest.MonkeyPatch) -> None:
    same = _fixture("dialog-bash.txt")
    _seen, sent = _wire(monkeypatch, [same, same])
    assert send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0) is ReplyOutcome.STILL_PRESENT
    assert sent == [("main:1.0", "1")]


def test_reply_ok_when_next_dialog_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    """送出後畫面換成「下一個」權限對話框 → 原請求已被回覆，算成功。"""
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-bash.txt"), _fixture("dialog-subagent.txt")])
    assert send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0) is ReplyOutcome.OK
    assert sent == [("main:1.0", "1")]


def test_reply_unverified_when_second_capture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-bash.txt"), None])
    assert send_permission_reply(TmuxBackend("main:1.0"), _dialog(), 1, delay=0) is ReplyOutcome.UNVERIFIED
    assert sent == [("main:1.0", "1")]


# ---------------------------------------------------------------------------
# tmux 封裝：subprocess 一律 mock
# ---------------------------------------------------------------------------


def test_capture_pane_returns_none_without_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert permission.capture_pane("main:1.0") is None
    assert permission.send_key("main:1.0", "1") is False


def test_capture_pane_runs_capture_command(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    class _Result:
        returncode = 0
        stdout = "screen"

    def fake_run(cmd: list[str], **kwargs: object) -> _Result:
        calls.append(cmd)
        return _Result()

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/tmux")
    monkeypatch.setattr(subprocess, "run", fake_run)
    assert permission.capture_pane("%12") == "screen"
    assert permission.send_key("%12", "2") is True
    assert calls == [
        ["tmux", "capture-pane", "-p", "-t", "%12"],
        ["tmux", "send-keys", "-t", "%12", "2"],
    ]


# ---------------------------------------------------------------------------
# iTerm2 backend：送鍵流程一樣走 send_permission_reply，只是 backend 換成 ITermBackend；
# osascript 一律 mock（不碰真 iTerm2）。
# ---------------------------------------------------------------------------


def _iterm_dialog() -> PermissionDialog:
    dialog = parse_permission_dialog(_fixture("iterm-dialog.txt"))
    assert dialog is not None
    return dialog


def _wire_osascript(monkeypatch: pytest.MonkeyPatch, responses: list[tuple[int, str, str]]) -> list[str]:
    """把 osascript 換成腳本：responses 依序回放，記錄每次送出的 script 原文。"""
    scripts: list[str] = []

    def fake_osascript(script: str) -> tuple[int, str, str]:
        scripts.append(script)
        return responses.pop(0) if responses else (1, "", "no more responses")

    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/osascript")
    monkeypatch.setattr(permission, "osascript", fake_osascript)
    return scripts


def test_iterm_reply_ok_when_dialog_disappears(monkeypatch: pytest.MonkeyPatch) -> None:
    """(a) capture 成功 → 整條回覆流程 OK：找到 session、送數字、驗證對話框消失。"""
    scripts = _wire_osascript(
        monkeypatch,
        [
            (0, _fixture("iterm-dialog.txt"), ""),
            (0, "ok", ""),
            (0, _fixture("iterm-after-reply.txt"), ""),
        ],
    )
    outcome = send_permission_reply(ITermBackend("/dev/ttys007"), _iterm_dialog(), 1, delay=0)
    assert outcome is ReplyOutcome.OK
    assert len(scripts) == 3
    assert 'write text "1" newline NO' in scripts[1]  # 單一數字、無 Enter


def test_iterm_reply_refuses_when_tty_session_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """(b) 找不到 tty 對應的 iTerm2 session → NO_DIALOG，不送鍵。"""
    scripts = _wire_osascript(monkeypatch, [(0, permission._ITERM_NO_SESSION, "")])
    outcome = send_permission_reply(ITermBackend("/dev/ttys999"), _iterm_dialog(), 1, delay=0)
    assert outcome is ReplyOutcome.NO_DIALOG
    assert len(scripts) == 1  # 只抓了一次畫面，沒送任何鍵


def test_iterm_reply_misfire_sends_backspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """(c) 誤送：數字落進聊天輸入框 → 補送 Backspace（ASCII 8，不帶 Enter）。"""
    scripts = _wire_osascript(
        monkeypatch,
        [
            (0, _fixture("iterm-dialog.txt"), ""),
            (0, "ok", ""),
            (0, _fixture("iterm-misfire.txt"), ""),
            (0, "ok", ""),
        ],
    )
    outcome = send_permission_reply(ITermBackend("/dev/ttys007"), _iterm_dialog(), 2, delay=0)
    assert outcome is ReplyOutcome.MISFIRE
    assert len(scripts) == 4
    assert 'write text "2" newline NO' in scripts[1]
    assert "write text (ASCII character 8) newline NO" in scripts[3]


def test_iterm_capture_returns_none_without_osascript(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert permission.iterm_capture("/dev/ttys007") is None
    assert permission.iterm_send_digit("/dev/ttys007", "1") is False


# ---------------------------------------------------------------------------
# backend 選擇（tui.py 用）：tmux 座標優先，其次 macOS 上有 tty 就用 iTerm2，都沒有 → None
# ---------------------------------------------------------------------------


def _session(**overrides: object) -> Session:
    base: dict[str, object] = {
        "session_id": "s1",
        "cwd": "/tmp/project",
        "status": Status.WAITING,
        "last_active": 0.0,
        "last_action": "",
        "source": "hook",
    }
    base.update(overrides)
    return Session(**base)  # type: ignore[arg-type]


def test_select_backend_prefers_tmux_target() -> None:
    """(d)-1 有 tmux_target → TmuxBackend，即使也有 tty。"""
    backend = select_backend(_session(tmux_target="main:1.0", tty="/dev/ttys007"))
    assert isinstance(backend, TmuxBackend)
    assert backend.target == "main:1.0"


def test_select_backend_prefers_tmux_pane_over_target() -> None:
    backend = select_backend(_session(tmux_target="main:1.0", tmux_pane="%7"))
    assert isinstance(backend, TmuxBackend)
    assert backend.target == "%7"  # 穩定的 pane id 優先於 target 座標


def test_select_backend_falls_back_to_iterm_on_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d)-2 沒有 tmux 座標，但有 tty 且平台是 macOS → ITermBackend。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    backend = select_backend(_session(tty="/dev/ttys007"))
    assert isinstance(backend, ITermBackend)
    assert backend.tty == "/dev/ttys007"


def test_select_backend_none_when_no_coordinates_at_all(monkeypatch: pytest.MonkeyPatch) -> None:
    """(d)-3 沒有 tmux 座標也沒有 tty → None（呼叫端走既有 toast 路徑）。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    assert select_backend(_session()) is None


def test_select_backend_none_on_non_macos_without_tmux(monkeypatch: pytest.MonkeyPatch) -> None:
    """有 tty 但平台不是 macOS → 不接（iTerm2 backend 僅支援 macOS）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    assert select_backend(_session(tty="/dev/ttys007")) is None


# ---------------------------------------------------------------------------
# kitty backend：抓畫面／送鍵一律 mock（_wire_kitty），select_backend 的定位結果
# 直接 monkeypatch `permission.kitty_resolve_window`（定位邏輯本身在 test_focus.py 測）。
# ---------------------------------------------------------------------------


def test_select_backend_no_kitty_installed_falls_back_to_iterm(monkeypatch: pytest.MonkeyPatch) -> None:
    """1. 沒裝 kitty（which 回 None）→ select_backend() 不回 KittyBackend；macOS ＋ tty 時仍回 ITermBackend。"""
    monkeypatch.setattr(permission, "kitty_resolve_window", focus_kitty.resolve_window)
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "platform", "darwin")
    backend = select_backend(_session(tty="/dev/ttys007"))
    assert isinstance(backend, ITermBackend)
    assert backend.tty == "/dev/ttys007"


def test_select_backend_kitty_cannot_resolve_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """2. 有 kitty 但配不到 window（kitty_resolve_window 回 None）→ macOS 仍回 ITermBackend；非 macOS 回 None。"""
    monkeypatch.setattr(sys, "platform", "darwin")
    backend = select_backend(_session(tty="/dev/ttys007"))
    assert isinstance(backend, ITermBackend)

    monkeypatch.setattr(sys, "platform", "linux")
    assert select_backend(_session(tty="/dev/ttys007")) is None


def test_select_backend_returns_kitty_when_resolved(monkeypatch: pytest.MonkeyPatch) -> None:
    """3. 配得到 window → 回 KittyBackend，且 socket_path / window_id 等於 resolve 的結果。"""
    monkeypatch.setattr(permission, "kitty_resolve_window", lambda tty: ("/tmp/kitty-1", 3))
    backend = select_backend(_session(tty="/dev/ttys007"))
    assert isinstance(backend, KittyBackend)
    assert backend.socket_path == "/tmp/kitty-1"
    assert backend.window_id == 3


def test_select_backend_prefers_tmux_even_when_kitty_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """4. tmux 座標存在時仍優先 TmuxBackend，即使 kitty 也配得到。"""
    monkeypatch.setattr(permission, "kitty_resolve_window", lambda tty: ("/tmp/kitty-1", 3))
    backend = select_backend(_session(tmux_target="main:1.0", tty="/dev/ttys007"))
    assert isinstance(backend, TmuxBackend)
    assert backend.target == "main:1.0"


def test_select_backend_kitty_not_limited_to_macos(monkeypatch: pytest.MonkeyPatch) -> None:
    """5. 非 macOS ＋ 配得到 kitty window → 回 KittyBackend（kitty 這關不被 darwin 判斷擋住）。"""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(permission, "kitty_resolve_window", lambda tty: ("/tmp/kitty-1", 3))
    backend = select_backend(_session(tty="/dev/ttys007"))
    assert isinstance(backend, KittyBackend)


def test_kitty_capture_argv_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """6. capture() 成功 → 回 fixture 內容，argv 逐字等於指令（沒有 --extent / --ansi）。"""
    calls = _wire_kitty(monkeypatch, [(0, _fixture("dialog-bash.txt"))])
    assert permission.kitty_capture("/tmp/kitty-1", 3) == _fixture("dialog-bash.txt")
    assert calls == [["kitty", "@", "--to", "unix:/tmp/kitty-1", "get-text", "--match", "id:3"]]


def test_kitty_capture_failure_returns_none_and_blocks_send(monkeypatch: pytest.MonkeyPatch) -> None:
    """7. get-text 非 0（或 which 回 None）→ capture() 回 None；接進 send_permission_reply()
    → NO_DIALOG 且完全沒有送鍵（argv 記錄裡沒有 send-text）。"""
    _wire_kitty(monkeypatch, [(1, "")])
    assert permission.kitty_capture("/tmp/kitty-1", 3) is None

    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert permission.kitty_capture("/tmp/kitty-1", 3) is None

    calls = _wire_kitty(monkeypatch, [(1, "")])
    backend = KittyBackend("/tmp/kitty-1", 3)
    outcome = send_permission_reply(backend, _dialog(), 1, delay=0)
    assert outcome is ReplyOutcome.NO_DIALOG
    assert not any("send-text" in arg for call in calls for arg in call)


def test_kitty_send_digit_argv_exact_no_newline(monkeypatch: pytest.MonkeyPatch) -> None:
    """8. send_digit("2") 的 argv 逐字等於指令；任何一個參數都不含 \\n。"""
    calls = _wire_kitty(monkeypatch, [(0, "")])
    assert permission.kitty_send_digit("/tmp/kitty-1", 3, "2") is True
    assert calls == [["kitty", "@", "--to", "unix:/tmp/kitty-1", "send-text", "--match", "id:3", "--", "2"]]
    assert not any("\n" in arg for arg in calls[0])


def test_kitty_send_backspace_argv_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    """9. send_backspace() 送出 send-key -- backspace（拍板 A 的位元組來源）。"""
    calls = _wire_kitty(monkeypatch, [(0, "")])
    assert permission.kitty_send_backspace("/tmp/kitty-1", 3) is True
    assert calls == [["kitty", "@", "--to", "unix:/tmp/kitty-1", "send-key", "--match", "id:3", "--", "backspace"]]


def test_kitty_full_flow_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    """10a. 整條流程：KittyBackend 跑 send_permission_reply(delay=0) → OK。"""
    _wire_kitty(
        monkeypatch,
        [
            (0, _fixture("dialog-bash.txt")),
            (0, ""),
            (0, _fixture("no-dialog-after-reply.txt")),
        ],
    )
    backend = KittyBackend("/tmp/kitty-1", 3)
    outcome = send_permission_reply(backend, _dialog(), 1, delay=0)
    assert outcome is ReplyOutcome.OK


def test_kitty_full_flow_misfire_sends_backspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """10b. 誤送：dialog-bash → no-dialog-misfire → MISFIRE，且有送出 backspace argv。"""
    calls = _wire_kitty(
        monkeypatch,
        [
            (0, _fixture("dialog-bash.txt")),
            (0, ""),
            (0, _fixture("no-dialog-misfire.txt")),
            (0, ""),
        ],
    )
    backend = KittyBackend("/tmp/kitty-1", 3)
    outcome = send_permission_reply(backend, _dialog(), 2, delay=0)
    assert outcome is ReplyOutcome.MISFIRE
    assert calls[-1] == ["kitty", "@", "--to", "unix:/tmp/kitty-1", "send-key", "--match", "id:3", "--", "backspace"]
