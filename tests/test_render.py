from rich.console import Console

from ring.cli import _render_plain, _rich_renderable
from ring.i18n import set_lang
from ring.registry import Session, Status


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
    for col in ("狀態", "專案", "進度", "閒置", "去哪", "動作"):
        assert col in out, f"missing column header {col}"


def test_plain_renderer_includes_header_and_data() -> None:
    set_lang("en")
    out = _render_plain(_sessions(), show_legend=True)
    assert "action" in out
    assert "maigo" in out
