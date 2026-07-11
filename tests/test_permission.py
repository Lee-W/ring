"""permission 模組：對話框解析（餵 PoC 真實畫面）與送鍵流程（mock tmux）。

fixtures 是 PoC（claude 2.1.206 + tmux 3.7b）用 ``tmux capture-pane -p`` 抓下來的
真實畫面，放 ``tests/fixtures/permission/``：

- ``dialog-bash.txt``：一般 Bash 權限對話框（3 個選項）
- ``dialog-subagent.txt``：背景 subagent 的對話框（標題帶 "from the general-purpose agent"）
- ``no-dialog-misfire.txt``：對話框不在時誤送「2」、數字落進聊天輸入框的樣子
- ``no-dialog-after-reply.txt``：回覆成功後對話框消失、模型繼續跑的畫面
"""

import shutil
import subprocess
from pathlib import Path

import pytest

import ring.permission as permission
from ring.permission import (
    PermissionDialog,
    ReplyOutcome,
    digit_in_input_line,
    parse_permission_dialog,
    send_permission_reply,
)

_FIXTURES = Path(__file__).parent / "fixtures" / "permission"


def _fixture(name: str) -> str:
    return (_FIXTURES / name).read_text(encoding="utf-8")


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
    outcome = send_permission_reply("main:1.0", _dialog(), 1, delay=0)
    assert outcome is ReplyOutcome.OK
    assert sent == [("main:1.0", "1")]  # 單一數字、無 Enter


def test_reply_refuses_when_no_dialog(monkeypatch: pytest.MonkeyPatch) -> None:
    """二次 capture 抓不到對話框 → 不送鍵。"""
    _seen, sent = _wire(monkeypatch, [_fixture("no-dialog-after-reply.txt")])
    assert send_permission_reply("main:1.0", _dialog(), 1, delay=0) is ReplyOutcome.NO_DIALOG
    assert sent == []


def test_reply_refuses_when_capture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _seen, sent = _wire(monkeypatch, [None])
    assert send_permission_reply("main:1.0", _dialog(), 1, delay=0) is ReplyOutcome.NO_DIALOG
    assert sent == []


def test_reply_refuses_when_dialog_changed(monkeypatch: pytest.MonkeyPatch) -> None:
    """二次 capture 的對話框內容變了（換成 subagent 的請求）→ 不送鍵。"""
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-subagent.txt")])
    assert send_permission_reply("main:1.0", _dialog(), 1, delay=0) is ReplyOutcome.CHANGED
    assert sent == []


def test_reply_refuses_number_outside_options(monkeypatch: pytest.MonkeyPatch) -> None:
    seen, sent = _wire(monkeypatch, [])
    assert send_permission_reply("main:1.0", _dialog(), 7, delay=0) is ReplyOutcome.CHANGED
    assert seen == [] and sent == []


def test_reply_misfire_sends_backspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """送鍵瞬間對話框消失、數字落進輸入框（❯ 2）→ 補 Backspace。"""
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-bash.txt"), _fixture("no-dialog-misfire.txt")])
    outcome = send_permission_reply("main:1.0", _dialog(), 2, delay=0)
    assert outcome is ReplyOutcome.MISFIRE
    assert sent == [("main:1.0", "2"), ("main:1.0", "BSpace")]


def test_reply_warns_when_dialog_still_present(monkeypatch: pytest.MonkeyPatch) -> None:
    same = _fixture("dialog-bash.txt")
    _seen, sent = _wire(monkeypatch, [same, same])
    assert send_permission_reply("main:1.0", _dialog(), 1, delay=0) is ReplyOutcome.STILL_PRESENT
    assert sent == [("main:1.0", "1")]


def test_reply_ok_when_next_dialog_appears(monkeypatch: pytest.MonkeyPatch) -> None:
    """送出後畫面換成「下一個」權限對話框 → 原請求已被回覆，算成功。"""
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-bash.txt"), _fixture("dialog-subagent.txt")])
    assert send_permission_reply("main:1.0", _dialog(), 1, delay=0) is ReplyOutcome.OK
    assert sent == [("main:1.0", "1")]


def test_reply_unverified_when_second_capture_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _seen, sent = _wire(monkeypatch, [_fixture("dialog-bash.txt"), None])
    assert send_permission_reply("main:1.0", _dialog(), 1, delay=0) is ReplyOutcome.UNVERIFIED
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
