import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

import ring.gc as gc
import ring.registry as registry


@pytest.fixture(autouse=True)
def _isolate_ipc_paths(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """GC tests must never inspect or delete the developer/runner's real RiNG state."""
    monkeypatch.setattr(gc, "_FOCUS_REQUEST_PATH", tmp_path / "focus-request")
    monkeypatch.setattr(gc, "_PRESENCE_PATH", tmp_path / "tui-presence")


def _write_session(path: Path, sid: str, *, last_active: float, provider: str = "claude-code") -> Path:
    path.mkdir(parents=True, exist_ok=True)
    f = path / f"{sid}.json"
    f.write_text(
        json.dumps(
            {
                "session_id": sid,
                "provider": provider,
                "cwd": "/work/app",
                "status": "waiting",
                "last_active": last_active,
                "last_action": "—",
            }
        ),
        encoding="utf-8",
    )
    return f


def test_parse_duration() -> None:
    assert gc.parse_duration("30s") == 30
    assert gc.parse_duration("2m") == 120
    assert gc.parse_duration("3h") == 10800
    assert gc.parse_duration("7d") == 604800
    assert gc.parse_duration("42") == 42


def test_gc_collects_only_old_ended_registry(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "sessions"
    now = time.time()
    old = _write_session(registry_dir, "old", last_active=now - 8 * 86400)
    new = _write_session(registry_dir, "new", last_active=now - 3600)

    monkeypatch.setattr(gc, "RING_REGISTRY", registry_dir)
    monkeypatch.setattr(registry, "RING_REGISTRY", registry_dir)
    monkeypatch.setitem(registry._PROVIDER_PROCS, "claude-code", lambda: [])
    monkeypatch.setattr(gc, "collect_provider_procs", lambda: {"claude-code": []})

    candidates = gc.collect_candidates(older_than=7 * 86400, now=now)

    assert [c.path for c in candidates] == [old]
    assert new.exists()


def test_gc_all_ended_ignores_age(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "sessions"
    now = time.time()
    new = _write_session(registry_dir, "new", last_active=now - 60)

    monkeypatch.setattr(gc, "RING_REGISTRY", registry_dir)
    monkeypatch.setattr(registry, "RING_REGISTRY", registry_dir)
    monkeypatch.setitem(registry._PROVIDER_PROCS, "claude-code", lambda: [])
    monkeypatch.setattr(gc, "collect_provider_procs", lambda: {"claude-code": []})

    candidates = gc.collect_candidates(older_than=7 * 86400, all_ended=True, now=now)

    assert [c.path for c in candidates] == [new]


def test_gc_dry_run_does_not_delete_session_start_phantom(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "sessions"
    phantom = _write_session(registry_dir, "startup:abc", last_active=0, provider="startup")

    monkeypatch.setattr(gc, "RING_REGISTRY", registry_dir)
    monkeypatch.setattr(registry, "RING_REGISTRY", registry_dir)
    monkeypatch.setitem(registry._PROVIDER_PROCS, "claude-code", lambda: [])
    monkeypatch.setattr(gc, "collect_provider_procs", lambda: {"claude-code": []})

    result = gc.run_gc(dry_run=True)

    assert result.candidates[0].path == phantom
    assert phantom.exists()


def test_gc_deletes_candidates(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "sessions"
    old = _write_session(registry_dir, "old", last_active=0)

    monkeypatch.setattr(gc, "RING_REGISTRY", registry_dir)
    monkeypatch.setattr(registry, "RING_REGISTRY", registry_dir)
    monkeypatch.setitem(registry._PROVIDER_PROCS, "claude-code", lambda: [])
    monkeypatch.setattr(gc, "collect_provider_procs", lambda: {"claude-code": []})

    result = gc.run_gc(older_than=1)

    assert result.deleted[0].path == old
    assert not old.exists()


def test_gc_prunes_hidden_sessions_not_found_or_too_old(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deleted_path = tmp_path / "deleted_sessions.json"
    now = time.time()
    deleted_path.write_text(
        json.dumps(
            {
                "keep-1": datetime.fromtimestamp(now - 10, UTC).isoformat(),  # 找得到、夠新 → 留
                "gone-1": datetime.fromtimestamp(now - 10, UTC).isoformat(),  # 哪個 source 都找不到 → 清
                "old-1": datetime.fromtimestamp(now - 1000, UTC).isoformat(),  # 找得到但超過保留期 → 清
            }
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(registry, "DELETED_SESSIONS", deleted_path)
    monkeypatch.setattr(gc, "_known_session_ids", lambda: {"keep-1", "old-1"})
    monkeypatch.setattr(gc, "RING_REGISTRY", tmp_path / "missing")
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "missing")

    result = gc.run_gc(older_than=100)

    assert set(result.hidden_stale) == {"gone-1", "old-1"}
    assert result.hidden_remaining == 1
    assert registry.hidden_session_ids(path=deleted_path) == {"keep-1"}


def test_gc_dry_run_previews_hidden_stale_without_removing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    deleted_path = tmp_path / "deleted_sessions.json"
    now = time.time()
    deleted_path.write_text(
        json.dumps({"gone-1": datetime.fromtimestamp(now - 10, UTC).isoformat()}),
        encoding="utf-8",
    )

    monkeypatch.setattr(registry, "DELETED_SESSIONS", deleted_path)
    monkeypatch.setattr(gc, "_known_session_ids", lambda: set())
    monkeypatch.setattr(gc, "RING_REGISTRY", tmp_path / "missing")
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "missing")

    result = gc.run_gc(older_than=100, dry_run=True)

    assert result.hidden_stale == ["gone-1"]
    # dry-run 只預覽，不真的清掉。
    assert registry.hidden_session_ids(path=deleted_path) == {"gone-1"}


def test_gc_skips_source_scan_when_nothing_hidden(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """隱藏清單本來就空 → 不該去真的掃 source（省一輪 process/檔案掃描）。"""
    monkeypatch.setattr(registry, "DELETED_SESSIONS", tmp_path / "deleted_sessions.json")

    def _boom() -> set[str]:
        raise AssertionError("_known_session_ids 不該被呼叫")

    monkeypatch.setattr(gc, "_known_session_ids", _boom)
    monkeypatch.setattr(gc, "RING_REGISTRY", tmp_path / "missing")
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "missing")

    result = gc.run_gc(older_than=100)

    assert result.hidden_stale == []
    assert result.hidden_remaining == 0


def test_gc_collects_stale_ipc_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    now = time.time()
    focus = tmp_path / "focus-request"
    presence = tmp_path / "tui-presence"
    focus.write_text(json.dumps({"session_id": "s1", "ts": now - 60}), encoding="utf-8")
    presence.write_text(json.dumps({"tty": "", "pid": 1, "ts": now - 600}), encoding="utf-8")

    monkeypatch.setattr(gc, "RING_REGISTRY", tmp_path / "missing")
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "missing")
    monkeypatch.setattr(gc, "_FOCUS_REQUEST_PATH", focus)
    monkeypatch.setattr(gc, "_PRESENCE_PATH", presence)

    candidates = gc.collect_candidates(older_than=7 * 86400, now=now)

    assert {c.path for c in candidates} == {focus, presence}
