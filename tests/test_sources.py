import pytest

import ring.sources as sources
from ring.registry import Session, Status


class _FakeSource:
    name = "mytool"

    def discover(self) -> list[Session]:
        return [Session("custom-1", "/work/x", Status.WORKING, 100.0, "→ doing", "mytool")]


def test_custom_source_sessions_appear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "_SOURCES", [_FakeSource()])
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {})
    result = sources.discover_sessions()
    assert any(s.session_id == "custom-1" and s.source == "mytool" for s in result)


def test_register_source_appends(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sources, "_SOURCES", list(sources._SOURCES))  # 別污染全域預設
    before = len(sources.sources())
    sources.register_source(_FakeSource())
    assert len(sources.sources()) == before + 1
    assert sources.sources()[-1].name == "mytool"


def test_default_source_is_claude_code() -> None:
    assert any(s.name == "claude-code" for s in sources.sources())
