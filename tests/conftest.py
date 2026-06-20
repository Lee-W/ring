from collections.abc import Iterator

import pytest

from ring.i18n import set_lang


@pytest.fixture(autouse=True)
def _reset_lang(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """每個 test 清掉 RING_LANG / LANG 並重設語言。

    這樣預設確定是台灣漢語、與執行環境的 locale 無關（CI runner 常設 LANG=en_US，
    否則 resolve_lang(None) 會被帶成 en，讓「預設＝中文」的測試在某些 runner 上爆掉）。
    """
    monkeypatch.delenv("RING_LANG", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    set_lang(None)
    yield
    set_lang(None)
