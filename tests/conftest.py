from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest

import ring.registry as registry
import ring.sources.local_llm as local_llm
from ring.i18n import set_lang


@pytest.fixture(autouse=True)
def _reset_lang(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """每個 test 清掉 RING_LANG / LANG 並重設語言。

    這樣預設確定是台灣漢語、與執行環境的 locale 無關（CI runner 常設 LANG=en_US，
    否則 resolve_lang(None) 會被帶成 en，讓「預設＝中文」的測試在某些 runner 上爆掉）。
    """
    monkeypatch.delenv("RING_LANG", raising=False)
    monkeypatch.delenv("LANG", raising=False)
    monkeypatch.setattr(registry, "CODEX_STATE", Path("/nonexistent/ring-test-codex-state.sqlite"))
    monkeypatch.setattr(registry, "DELETED_SESSIONS", tmp_path / "deleted_sessions.json")
    set_lang(None)
    yield
    set_lang(None)


@pytest.fixture(autouse=True)
def _no_real_local_llm_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """local-LLM 來源會真的呼叫 ``ps`` 掃這台機器上的行程；預設回空，讓舊測試不受
    這台機器當下是否真的跑著 ``ollama run`` / ``llama-cli`` 影響（同 ``CODEX_STATE``
    指向不存在路徑的隔離手法）。

    只換掉 ``ring.sources.local_llm`` 模組自己名字空間裡的 ``subprocess`` 綁定
    （``monkeypatch.setattr(local_llm, "subprocess", ...)``），不是改
    ``subprocess.run`` 這個共用模組屬性本身——後者會讓行程裡任何其他呼叫
    ``subprocess.run`` 的程式碼（例如 ``tests/test_focus.py`` 真的呼叫 ps／nvim）
    也被攔截，汙染整個測試行程。``tests/test_local_llm.py`` 會在各自測試裡用同一個
    ``monkeypatch`` 再次對這個（此時已是 fake 的）``subprocess`` 物件設定 ``.run``，
    行為不受影響。
    """
    monkeypatch.setattr(
        local_llm,
        "subprocess",
        SimpleNamespace(run=lambda *_args, **_kwargs: SimpleNamespace(stdout="", returncode=0)),
    )
