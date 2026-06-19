from collections.abc import Iterator

import pytest

from ring.i18n import set_lang


@pytest.fixture(autouse=True)
def _reset_lang() -> Iterator[None]:
    """每個 test 前後把語言重設回預設（台灣漢語），避免全域 locale 互相污染。"""
    set_lang(None)
    yield
    set_lang(None)
