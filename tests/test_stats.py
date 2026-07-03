"""ring stats——狀態轉換 log 與等待統計。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import ring.hook as hook
import ring.stats as stats
from ring.config import Config
from ring.stats import aggregate, collect_waits, log_transition


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ring.hook.get_config", lambda: Config())
    monkeypatch.setattr("ring.notify._NOTIFIERS", [])
    monkeypatch.setattr("ring.stats.EVENTS_PATH", tmp_path / "events.jsonl")


def _feed(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


# ---------------------------------------------------------------------------
# log_transition
# ---------------------------------------------------------------------------


def test_log_transition_appends_jsonl(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "events.jsonl"
    log_transition("s1", "claude-code", "/x/maigo", "waiting", path=p, now=100.0)
    log_transition("s1", "claude-code", "/x/maigo", "working", path=p, now=110.0)
    lines = [json.loads(line) for line in p.read_text().splitlines()]
    assert [line["status"] for line in lines] == ["waiting", "working"]
    assert lines[0]["ts"] == 100.0
    assert lines[0]["cwd"] == "/x/maigo"


def test_log_trims_oversized_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(stats, "_MAX_BYTES", 200)
    p = tmp_path / "events.jsonl"
    for i in range(20):
        log_transition("s1", "claude-code", "/x", "working", path=p, now=float(i))
    lines = p.read_text().splitlines()
    assert len(lines) < 20  # 有砍半過
    assert json.loads(lines[-1])["ts"] == 19.0  # 保留最新


# ---------------------------------------------------------------------------
# collect_waits / aggregate
# ---------------------------------------------------------------------------


def _log(p: Path, sid: str, status: str, ts: float, cwd: str = "/x/maigo") -> None:
    log_transition(sid, "claude-code", cwd, status, path=p, now=ts)


def test_collect_waits_pairs_waiting_with_next_transition(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    _log(p, "s1", "working", 100.0)
    _log(p, "s1", "waiting", 200.0)
    _log(p, "s1", "working", 245.0)

    spans = collect_waits(10_000, path=p, now=1000.0)
    assert len(spans) == 1
    assert spans[0].seconds == 45.0
    assert spans[0].project == "maigo"
    assert spans[0].ongoing is False


def test_collect_waits_open_span_counts_to_now(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    _log(p, "s1", "waiting", 900.0)
    spans = collect_waits(10_000, path=p, now=1000.0)
    assert len(spans) == 1
    assert spans[0].seconds == 100.0
    assert spans[0].ongoing is True


def test_collect_waits_respects_since_window(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    _log(p, "s1", "waiting", 100.0)
    _log(p, "s1", "working", 150.0)
    _log(p, "s2", "waiting", 950.0)
    _log(p, "s2", "working", 960.0)

    spans = collect_waits(100, path=p, now=1000.0)  # 只看最近 100s
    assert [s.session_id for s in spans] == ["s2"]


def test_collect_waits_ignores_garbage_lines(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    _log(p, "s1", "waiting", 900.0)
    with p.open("a") as f:
        f.write('not json\n{"ts": "nope"}\n')
    assert len(collect_waits(10_000, path=p, now=1000.0)) == 1


def test_aggregate_by_project_sorted_by_total(tmp_path: Path) -> None:
    p = tmp_path / "e.jsonl"
    _log(p, "a1", "waiting", 100.0, cwd="/x/maigo")
    _log(p, "a1", "working", 110.0, cwd="/x/maigo")
    _log(p, "a1", "waiting", 200.0, cwd="/x/maigo")
    _log(p, "a1", "working", 230.0, cwd="/x/maigo")
    _log(p, "b1", "waiting", 300.0, cwd="/y/blog")
    _log(p, "b1", "idle", 400.0, cwd="/y/blog")

    out = aggregate(collect_waits(10_000, path=p, now=1000.0))
    assert [s.project for s in out] == ["blog", "maigo"]  # blog 總等待 100s > maigo 40s
    maigo = out[1]
    assert maigo.waits == 2
    assert maigo.total_seconds == 40.0
    assert maigo.max_seconds == 30.0
    assert maigo.avg_seconds == 20.0


# ---------------------------------------------------------------------------
# hook 端：只在狀態轉換時記 log
# ---------------------------------------------------------------------------


def test_hook_logs_only_transitions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path / "reg")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr("ring.stats.EVENTS_PATH", events)

    for event, n in (("UserPromptSubmit", 1), ("PostToolUse", 1), ("PermissionRequest", 2), ("Stop", 3)):
        _feed(monkeypatch, {"session_id": "s1", "hook_event_name": event, "cwd": "/x/maigo"})
        assert hook.run_hook() == 0
        lines = events.read_text().splitlines() if events.exists() else []
        assert len(lines) == n  # PostToolUse 維持 working → 不記

    statuses = [json.loads(line)["status"] for line in events.read_text().splitlines()]
    assert statuses == ["working", "waiting", "idle"]


def test_hook_logs_ended_on_session_end(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path / "reg")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr("ring.stats.EVENTS_PATH", events)

    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": "/x"})
    assert hook.run_hook() == 0
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "SessionEnd", "cwd": "/x"})
    assert hook.run_hook() == 0

    statuses = [json.loads(line)["status"] for line in events.read_text().splitlines()]
    assert statuses == ["working", "ended"]


def test_hook_session_end_without_registry_logs_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """從沒見過的 session 直接 SessionEnd → 沒有前狀態，不記幽靈 ended。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path / "reg")
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr("ring.stats.EVENTS_PATH", events)

    _feed(monkeypatch, {"session_id": "ghost", "hook_event_name": "SessionEnd", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert not events.exists()
