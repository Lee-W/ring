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


def test_default_sources_include_claude_code_and_codex() -> None:
    assert any(s.name == "claude-code" for s in sources.sources())
    assert any(s.name == "codex" for s in sources.sources())


class _StaticSource:
    def __init__(self, name: str, sessions: list[Session]) -> None:
        self.name = name
        self._sessions = sessions

    def discover(self) -> list[Session]:
        return self._sessions


def test_newer_scan_clears_stale_hook_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_session = Session(
        "same-id",
        "/work/app",
        Status.WAITING,
        100.0,
        "permission",
        "hook",
        tty="/dev/ttys001",
        provider="claude-code",
    )
    scan_session = Session(
        "same-id",
        "/work/app",
        Status.WORKING,
        110.0,
        "user replied",
        "scan",
        provider="claude-code",
    )
    monkeypatch.setattr(
        sources,
        "_SOURCES",
        [_StaticSource("hook", [hook_session]), _StaticSource("scan", [scan_session])],
    )
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {})

    result = sources.discover_sessions()

    assert len(result) == 1
    assert result[0].source == "scan"
    assert result[0].status is Status.WORKING
    assert result[0].tty == "/dev/ttys001"


def test_older_scan_does_not_clear_hook_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    hook_session = Session("same-id", "/work/app", Status.WAITING, 100.0, "permission", "hook", provider="claude-code")
    scan_session = Session("same-id", "/work/app", Status.IDLE, 90.0, "older", "scan", provider="claude-code")
    monkeypatch.setattr(
        sources,
        "_SOURCES",
        [_StaticSource("hook", [hook_session]), _StaticSource("scan", [scan_session])],
    )
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {})

    result = sources.discover_sessions()

    assert len(result) == 1
    assert result[0].source == "hook"
    assert result[0].status is Status.WAITING
