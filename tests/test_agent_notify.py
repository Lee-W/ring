"""``ring.agent_notify``：Claude Code 自帶通知通道的唯讀診斷。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ring.agent_notify import DEFAULT_CHANNEL, detect_terminal, native_notify_status


def _write(path: Path, data: object) -> Path:
    target = path / "settings.json"
    target.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return target


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"KITTY_WINDOW_ID": "3"}, "kitty"),
        ({"TERM": "xterm-kitty"}, "kitty"),
        ({"TERM_PROGRAM": "iTerm.app"}, "iTerm2"),
        ({"TERM_PROGRAM": "Apple_Terminal"}, "Terminal"),
        ({"TERM_PROGRAM": "ghostty"}, "ghostty"),
        # 認不出的 TERM_PROGRAM 原樣回傳，讓 doctor 至少印得出看到的是什麼
        ({"TERM_PROGRAM": "SomeNewTerm"}, "SomeNewTerm"),
        ({}, ""),
        ({"TERM": "screen-256color"}, ""),
    ],
)
def test_detect_terminal(env: dict[str, str], expected: str) -> None:
    assert detect_terminal(env) == expected


def test_kitty_marker_wins_over_term_program() -> None:
    """kitty 底下 TERM_PROGRAM 可能殘留外層終端的值 → kitty 標記優先。"""
    assert detect_terminal({"KITTY_WINDOW_ID": "1", "TERM_PROGRAM": "iTerm.app"}) == "kitty"


def test_missing_settings_file_falls_back_to_auto(tmp_path: Path) -> None:
    """settings 檔不存在 → 當作沒設定（auto），且 exists=False。"""
    st = native_notify_status(tmp_path / "nope.json", {"TERM": "xterm-kitty"})
    assert st.exists is False
    assert st.channel == DEFAULT_CHANNEL
    assert st.explicit is False
    assert st.kind == "banner"
    assert st.duplicates_ring is True


def test_unset_channel_in_kitty_is_banner(tmp_path: Path) -> None:
    """有 settings 檔但沒設 preferredNotifChannel → auto；kitty 底下會跳通知。"""
    p = _write(tmp_path, {"model": "opus", "hooks": {}})
    st = native_notify_status(p, {"TERM": "xterm-kitty"})
    assert st.exists is True
    assert (st.channel, st.explicit, st.kind) == (DEFAULT_CHANNEL, False, "banner")


def test_auto_in_unknown_terminal_is_unknown(tmp_path: Path) -> None:
    """auto ＋ 認不出的終端 → 不假裝知道會不會跳，但仍算「可能重複」。"""
    p = _write(tmp_path, {})
    st = native_notify_status(p, {})
    assert st.kind == "unknown"
    assert st.duplicates_ring is True


def test_explicit_auto_is_treated_like_unset(tmp_path: Path) -> None:
    """明確寫 "auto" 跟沒設定同義——都要看終端才知道結果。"""
    p = _write(tmp_path, {"preferredNotifChannel": "auto"})
    st = native_notify_status(p, {"TERM_PROGRAM": "iTerm.app"})
    assert st.explicit is True
    assert st.kind == "banner"


@pytest.mark.parametrize(
    ("channel", "kind", "duplicates"),
    [
        ("notifications_disabled", "off", False),
        ("terminal_bell", "bell", True),
        ("kitty", "banner", True),
        ("ghostty", "banner", True),
        ("iterm2", "banner", True),
        ("iterm2_with_bell", "banner", True),
        ("some_future_channel", "unknown", True),
    ],
)
def test_explicit_channel_kinds(tmp_path: Path, channel: str, kind: str, duplicates: bool) -> None:
    p = _write(tmp_path, {"preferredNotifChannel": channel})
    st = native_notify_status(p, {})
    assert (st.channel, st.explicit) == (channel, True)
    assert st.kind == kind
    assert st.duplicates_ring is duplicates


@pytest.mark.parametrize(
    "content",
    ["{ not json", "[]", '{"preferredNotifChannel": 3}', '{"preferredNotifChannel": "  "}'],
)
def test_broken_settings_never_raises(tmp_path: Path, content: str) -> None:
    """壞掉的 settings 檔一律當「沒設定」——doctor 是唯讀診斷，不該因為它掛掉。"""
    p = tmp_path / "settings.json"
    p.write_text(content)
    st = native_notify_status(p, {})
    assert st.exists is True
    assert (st.channel, st.explicit) == (DEFAULT_CHANNEL, False)
