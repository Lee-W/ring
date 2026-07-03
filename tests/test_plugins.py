"""plugin 載入器：entry point 與 config 兩條路都要能把外部註冊碼跑起來。"""

from __future__ import annotations

from pathlib import Path

import pytest

import ring.plugins as plugins
from ring.config import Config


@pytest.fixture(autouse=True)
def _fresh_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    plugins._reset_for_tests()
    # 預設不吃使用者真實 config / entry points；個別測試再覆寫。
    monkeypatch.setattr(plugins, "get_config", lambda: Config())
    monkeypatch.setattr(plugins, "entry_points", lambda group: [])


class _FakeEntryPoint:
    def __init__(self, name: str, obj: object, *, raises: bool = False) -> None:
        self.name = name
        self._obj = obj
        self._raises = raises

    def load(self) -> object:
        if self._raises:
            raise ImportError("boom")
        return self._obj


def test_entry_point_callable_is_invoked(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    ep = _FakeEntryPoint("mytool", lambda: calls.append("registered"))
    monkeypatch.setattr(plugins, "entry_points", lambda group: [ep])
    assert plugins.load_plugins() == ["mytool"]
    assert calls == ["registered"]


def test_entry_point_module_object_needs_no_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """entry point 指向模組時，import（load）本身就是註冊，不會被當 callable 呼叫。"""
    import types

    mod = types.ModuleType("fake_plugin_mod")
    ep = _FakeEntryPoint("modplug", mod)
    monkeypatch.setattr(plugins, "entry_points", lambda group: [ep])
    assert plugins.load_plugins() == ["modplug"]


def test_broken_entry_point_warns_but_does_not_raise(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    ep_bad = _FakeEntryPoint("bad", None, raises=True)
    ep_ok = _FakeEntryPoint("ok", lambda: None)
    monkeypatch.setattr(plugins, "entry_points", lambda group: [ep_bad, ep_ok])
    assert plugins.load_plugins() == ["ok"]
    assert "bad" in capsys.readouterr().err


def test_config_plugins_are_imported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """config 的 plugins 模組被 import，模組內的 register_*() 因而生效。"""
    mod = tmp_path / "ring_test_local_plugin.py"
    mod.write_text("import ring.sources\nLOADED = True\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(plugins, "get_config", lambda: Config(plugins=("ring_test_local_plugin",)))
    assert plugins.load_plugins() == ["ring_test_local_plugin"]

    import ring_test_local_plugin  # type: ignore[import-not-found]

    assert ring_test_local_plugin.LOADED is True


def test_missing_config_plugin_warns(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(plugins, "get_config", lambda: Config(plugins=("nope_not_a_module",)))
    assert plugins.load_plugins() == []
    assert "nope_not_a_module" in capsys.readouterr().err


def test_load_plugins_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    ep = _FakeEntryPoint("once", lambda: calls.append("x"))
    monkeypatch.setattr(plugins, "entry_points", lambda group: [ep])
    assert plugins.load_plugins() == ["once"]
    assert plugins.load_plugins() == []  # 第二次不重複註冊
    assert calls == ["x"]
