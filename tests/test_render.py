from __future__ import annotations

from collections.abc import Callable

import pytest
from rich.console import Console

from ring.cli import _LOC_MAX, _middle_truncate, _render_plain, _rich_renderable, show_tool_column
from ring.i18n import set_lang
from ring.registry import Session, Status


def test_show_tool_column_only_when_providers_differ() -> None:
    same = [
        Session("a", "/x", Status.WORKING, 0.0, "-", "hook", provider="claude-code"),
        Session("b", "/y", Status.IDLE, 0.0, "-", "hook", provider="claude-code"),
    ]
    mixed = [
        Session("a", "/x", Status.WORKING, 0.0, "-", "hook", provider="claude-code"),
        Session("b", "/y", Status.IDLE, 0.0, "-", "codex", provider="codex"),
    ]
    assert show_tool_column(same) is False
    assert show_tool_column(mixed) is True


def test_plain_hides_tool_column_when_uniform() -> None:
    set_lang("zh-Hant")
    ss = [Session("a", "/x/maigo", Status.WORKING, 0.0, "→ Edit", "hook", provider="claude-code")]
    assert "工具" not in _render_plain(ss, show_legend=False, show_tool=False)
    assert "工具" in _render_plain(ss, show_legend=False, show_tool=True)


def _sessions() -> list[Session]:
    long_action = "你好" * 60  # 故意很長，重現「action 吃掉整列把其他欄壓成 0」的 bug
    return [
        Session("a", "/x/maigo", Status.WORKING, 0.0, "→ Edit", "scan"),
        Session("b", "/y/blog", Status.ENDED, 0.0, long_action, "scan"),
    ]


def test_rich_renderable_keeps_all_columns_with_long_action() -> None:
    """regression：超長 action 不該讓其他欄消失（max_width 上限救場）。"""
    set_lang("zh-Hant")
    console = Console(width=130, record=True)
    console.print(_rich_renderable(_sessions(), show_legend=True))
    out = console.export_text()
    for col in ("狀態", "工具", "專案", "進度", "閒置", "去哪", "動作"):
        assert col in out, f"missing column header {col}"


def test_plain_renderer_includes_header_and_data() -> None:
    set_lang("en")
    out = _render_plain(_sessions(), show_legend=True)
    assert "action" in out
    assert "maigo" in out


@pytest.mark.parametrize(
    ("text", "max_len", "check"),
    [
        pytest.param(
            "~/ring",
            _LOC_MAX,
            lambda r: r == "~/ring",
            id="a-short-string-unchanged",
        ),
        pytest.param(
            "main:1.0",
            _LOC_MAX,
            lambda r: r == "main:1.0",
            id="b-tmux-coord-unchanged",
        ),
        pytest.param(
            "~/Programming/personal/aaaaaaaaaa/bbbbbbbbbb/cccccccc/ring",
            _LOC_MAX,
            lambda r: len(r) <= _LOC_MAX and "…" in r and r.endswith("/ring") and r.startswith("~"),
            id="c-long-path-preserves-tail",
        ),
        pytest.param(
            "~/" + "x" * (_LOC_MAX - 2) + "y",
            _LOC_MAX,
            lambda r: len(r) <= _LOC_MAX,
            id="d-boundary-over-max-len",
        ),
        pytest.param(
            "x" * (_LOC_MAX + 20),
            _LOC_MAX,
            lambda r: len(r) == _LOC_MAX and "…" in r,
            id="e-long-tail-fallback",
        ),
        pytest.param(
            "abcde",
            2,
            lambda r: len(r) <= 2,
            id="f-max-len-2-fallback-back-zero",
        ),
    ],
)
def test_middle_truncate(text: str, max_len: int, check: Callable[[str], bool]) -> None:
    result = _middle_truncate(text, max_len)
    assert check(result), f"_middle_truncate({text!r}, {max_len}) => {result!r} failed check"


def test_rich_renderable_keeps_all_columns_with_long_location() -> None:
    """regression: 超長 location 不該讓動作欄消失。

    Session.location property 在沒有 tmux_target 時回傳 cwd（home 縮成 ~）。
    塞超長 cwd 即可讓 location 超長，驗證動作欄不被壓掉。
    """
    set_lang("zh-Hant")
    long_cwd = "/x/" + "deep/" * 40 + "ring"  # location 會是這個超長路徑
    short_action = "→ Edit file.py"
    long_loc_session = Session("c", long_cwd, Status.WORKING, 0.0, short_action, "scan")

    console = Console(width=130, record=True)
    console.print(_rich_renderable([long_loc_session], show_legend=True))
    out = console.export_text()
    for col in ("狀態", "工具", "專案", "進度", "閒置", "去哪", "動作"):
        assert col in out, f"missing column header {col}"
    assert short_action in out, "action column was squeezed out by long location"
