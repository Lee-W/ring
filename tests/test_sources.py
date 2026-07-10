import pytest

import ring.sources as sources
from ring.registry import Session, Status


@pytest.fixture(autouse=True)
def _no_tmux_process_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.registry._tmux_pane_targets", lambda: {})
    monkeypatch.setattr("ring.registry._tmux_process_tree_targets", lambda sessions: {})
    monkeypatch.setattr("ring.registry._tmux_targets_by_cwd", lambda: {})


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


def test_hidden_sessions_are_filtered_out(monkeypatch: pytest.MonkeyPatch) -> None:
    """隱藏之後沒有新活動（last_active <= hidden_at）→ 仍不收進看板。"""
    monkeypatch.setattr(
        sources,
        "_SOURCES",
        [
            _StaticSource("hook", [Session("hidden-1", "/work/x", Status.WAITING, 100.0, "→ wait", "hook")]),
            _StaticSource("scan", [Session("visible-1", "/work/y", Status.IDLE, 90.0, "—", "scan")]),
        ],
    )
    monkeypatch.setattr("ring.registry.hidden_sessions", lambda: {"hidden-1": 200.0})
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {})

    result = sources.discover_sessions()

    assert [s.session_id for s in result] == ["visible-1"]


def test_hidden_session_auto_revives_on_newer_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    """隱藏之後有比 hidden_at 更新的活動（scan-only session 也算）→ 自動解除隱藏並收進看板。"""
    monkeypatch.setattr(
        sources,
        "_SOURCES",
        [_StaticSource("scan", [Session("revived-1", "/work/z", Status.WORKING, 150.0, "→ doing", "scan")])],
    )
    monkeypatch.setattr("ring.registry.hidden_sessions", lambda: {"revived-1": 100.0})
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {})

    unhidden: list[str] = []
    monkeypatch.setattr("ring.registry.unhide_session", lambda sid: unhidden.append(sid))

    result = sources.discover_sessions()

    assert [s.session_id for s in result] == ["revived-1"]
    assert unhidden == ["revived-1"]


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


def test_newer_scan_bookkeeping_write_does_not_clear_hook_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """權限請求後 Claude Code 補寫的簿記紀錄（last-prompt/ai-title/mode 等）推進了 mtime，
    但對話尾仍是 interrupted（工具呼叫進行中，使用者沒有真的回應）——不該清掉 WAITING。

    這是症狀 2 的反例：只看 last_active 先後、不看 _tail_kind 就會把貨真價實的
    「等你回應權限請求」誤判成「使用者已回應」，讓 🔴 WAITING 被靜默蓋掉。
    """
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
        110.0,  # 比 hook 的 last_active 新——但只是簿記寫入推進的 mtime
        "bookkeeping write",
        "scan",
        provider="claude-code",
    )
    scan_session._tail_kind = "interrupted"  # 對話尾仍是工具呼叫進行中，使用者沒有真的回應
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


def test_hook_tmux_pane_binding_wins_over_cwd_collision(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [
        Session(
            "hook-a",
            "/work/app",
            Status.WAITING,
            100.0,
            "permission",
            "hook",
            tmux_pane="%1",
        ),
        Session(
            "hook-b",
            "/work/app",
            Status.WAITING,
            90.0,
            "permission",
            "hook",
            tmux_pane="%2",
        ),
    ]
    monkeypatch.setattr(sources, "_SOURCES", [_StaticSource("hook", sessions)])
    monkeypatch.setattr("ring.registry.hidden_sessions", lambda: {})
    monkeypatch.setattr("ring.registry._tmux_pane_targets", lambda: {"%1": "main:1.0", "%2": "main:1.1"})
    monkeypatch.setattr("ring.registry._tmux_process_tree_targets", lambda sessions: {})
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {"/work/app": "main:1.0"})
    monkeypatch.setattr("ring.registry._tmux_targets_by_cwd", lambda: {"/work/app": ["main:1.0", "main:1.1"]})

    result = sources.discover_sessions()
    by_id = {s.session_id: s.tmux_target for s in result}

    assert by_id == {"hook-a": "main:1.0", "hook-b": "main:1.1"}


def test_dead_tmux_pane_binding_falls_back_to_cwd(monkeypatch: pytest.MonkeyPatch) -> None:
    session = Session(
        "hook-a",
        "/work/app",
        Status.WAITING,
        100.0,
        "permission",
        "hook",
        tmux_pane="%dead",
    )
    monkeypatch.setattr(sources, "_SOURCES", [_StaticSource("hook", [session])])
    monkeypatch.setattr("ring.registry.hidden_sessions", lambda: {})
    monkeypatch.setattr("ring.registry._tmux_pane_targets", lambda: {})
    monkeypatch.setattr("ring.registry._tmux_process_tree_targets", lambda sessions: {})
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {"/work/app": "main:1.0"})
    monkeypatch.setattr("ring.registry._tmux_targets_by_cwd", lambda: {"/work/app": ["main:1.0"]})

    result = sources.discover_sessions()

    assert result[0].tmux_target == "main:1.0"


def test_same_cwd_scan_sessions_get_distinct_fallback_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    sessions = [
        Session("scan-a", "/work/app", Status.WAITING, 100.0, "permission", "scan"),
        Session("scan-b", "/work/app", Status.WORKING, 90.0, "doing", "scan"),
    ]
    monkeypatch.setattr(sources, "_SOURCES", [_StaticSource("scan", sessions)])
    monkeypatch.setattr("ring.registry.hidden_sessions", lambda: {})
    monkeypatch.setattr("ring.registry._tmux_pane_targets", lambda: {})
    monkeypatch.setattr("ring.registry._tmux_process_tree_targets", lambda sessions: {})
    monkeypatch.setattr("ring.registry._tmux_targets", lambda: {"/work/app": "main:1.0"})
    monkeypatch.setattr("ring.registry._tmux_targets_by_cwd", lambda: {"/work/app": ["main:1.0", "main:1.1"]})

    result = sources.discover_sessions()
    by_id = {s.session_id: s.tmux_target for s in result}

    assert by_id == {"scan-a": "main:1.0", "scan-b": "main:1.1"}
