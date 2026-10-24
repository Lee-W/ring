"""transcript 解析快取：命中（同 mtime/size 不重讀）、失效（檔案變了重讀）、淘汰（本輪沒見到的清掉）。"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

import pytest

import ring.registry as registry
import ring.transcript as transcript
from ring.transcript import ParseCache, head_cwd_cached


def _claude_line(cwd: str, tool: str = "Edit") -> str:
    return json.dumps({"type": "assistant", "cwd": cwd, "message": {"content": [{"type": "tool_use", "name": tool}]}})


def _write_claude(projects: Path, sid: str, cwd: str, mtime: float, tool: str = "Edit") -> Path:
    d = projects / "-work-app"
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    f.write_text(_claude_line(cwd, tool) + "\n")
    os.utime(f, (mtime, mtime))
    return f


class _Counter:
    """包住 ``_tail_records`` / ``_head_cwd``，記錄每個 path 被實際讀了幾次。"""

    def __init__(self) -> None:
        self.tails: list[Path] = []
        self.heads: list[Path] = []


@pytest.fixture
def reads(monkeypatch: pytest.MonkeyPatch) -> _Counter:
    counter = _Counter()
    real_tail, real_head = transcript._tail_records, transcript._head_cwd

    def tail(path: Path, *a: Any, **kw: Any) -> list[dict[str, Any]]:
        counter.tails.append(path)
        return real_tail(path, *a, **kw)

    def head(path: Path, *a: Any, **kw: Any) -> str:
        counter.heads.append(path)
        return real_head(path, *a, **kw)

    monkeypatch.setattr(transcript, "_tail_records", tail)
    monkeypatch.setattr(transcript, "_head_cwd", head)
    monkeypatch.setattr(registry, "_tail_records", tail)
    return counter


@pytest.fixture
def projects(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    p = tmp_path / "projects"
    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", p)
    return p


def test_claude_scan_cache_hit_does_not_reread_unchanged_file(projects: Path, reads: _Counter) -> None:
    f = _write_claude(projects, "s1", "/work/app", time.time())

    first = registry._scan_sessions([])
    second = registry._scan_sessions([])

    assert reads.tails == [f]
    assert reads.heads == [f]
    assert [(s.cwd, s.last_action, s.origin_cwd) for s in first] == [
        (s.cwd, s.last_action, s.origin_cwd) for s in second
    ]


def test_claude_scan_cache_invalidates_when_mtime_changes(projects: Path, reads: _Counter) -> None:
    now = time.time()
    f = _write_claude(projects, "s1", "/work/app", now, tool="Edit")
    assert registry._scan_sessions([])[0].last_action == "→ Edit"

    f.write_text(_claude_line("/work/app", "Bash") + "\n")  # 同 size 但 mtime 不同
    os.utime(f, (now + 5, now + 5))
    after = registry._scan_sessions([])

    assert after[0].last_action == "→ Bash"
    assert reads.tails == [f, f]
    assert reads.heads == [f], "檔頭只以 path 為 key，內容追加後不必重讀"


def test_claude_scan_cache_invalidates_when_size_changes_with_same_mtime(projects: Path, reads: _Counter) -> None:
    now = time.time()
    f = _write_claude(projects, "s1", "/work/app", now)
    registry._scan_sessions([])

    with f.open("a") as fh:
        fh.write(_claude_line("/work/app", "Write") + "\n")
    os.utime(f, (now, now))  # mtime 還原，只剩 size 不同
    registry._scan_sessions([])

    assert len(reads.tails) == 2


def test_claude_scan_evicts_entries_not_seen_in_this_scan(projects: Path, reads: _Counter) -> None:
    now = time.time()
    keep = _write_claude(projects, "keep", "/work/app", now)
    gone = _write_claude(projects, "gone", "/work/app", now)
    registry._scan_sessions([])
    assert len(transcript.CLAUDE_TAIL_CACHE) == 2

    gone.unlink()
    registry._scan_sessions([])

    assert len(transcript.CLAUDE_TAIL_CACHE) == 1
    assert gone not in transcript._HEAD_CWD_CACHE
    assert keep in transcript._HEAD_CWD_CACHE


def test_claude_scan_evicts_old_version_of_changed_file(projects: Path) -> None:
    now = time.time()
    f = _write_claude(projects, "s1", "/work/app", now)
    registry._scan_sessions([])
    os.utime(f, (now + 5, now + 5))
    registry._scan_sessions([])

    assert len(transcript.CLAUDE_TAIL_CACHE) == 1, "同檔的舊 (mtime,size) 版本要在掃描結束時淘汰"


def test_claude_scan_does_not_leak_mutations_across_polls(projects: Path) -> None:
    _write_claude(projects, "s1", "/work/app", time.time())
    first = registry._scan_sessions([])
    first[0].recent_actions.append("polluted")

    assert "polluted" not in registry._scan_sessions([])[0].recent_actions


def test_head_cwd_cached_does_not_cache_empty_result(tmp_path: Path, reads: _Counter) -> None:
    f = tmp_path / "x.jsonl"
    f.write_text("")
    assert head_cwd_cached(f) == ""
    f.write_text(_claude_line("/late") + "\n")

    assert head_cwd_cached(f) == "/late"
    assert head_cwd_cached(f) == "/late"
    assert reads.heads == [f, f]


def test_parse_cache_get_on_missing_file_is_not_cached(tmp_path: Path) -> None:
    cache: ParseCache[int] = ParseCache(lambda _p: 1)
    cache.begin_scan()
    assert cache.get(tmp_path / "nope") == 1
    assert len(cache) == 0


def _write_codex_db(db: Path, rows: list[tuple[str, str, Path]]) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            "create table threads (id text primary key, cwd text, title text, rollout_path text,"
            " preview text default '', updated_at integer, updated_at_ms integer, archived integer default 0)"
        )
        now_ms = int(time.time() * 1000)
        con.executemany(
            "insert into threads (id, cwd, title, rollout_path, updated_at, updated_at_ms) values (?,?,?,?,?,?)",
            [(i, cwd, "t", str(rp), now_ms // 1000, now_ms) for i, cwd, rp in rows],
        )
        con.commit()
    finally:
        con.close()


def _write_rollout(path: Path, *events: str) -> None:
    path.write_text("\n".join(json.dumps({"type": "event_msg", "payload": {"type": e}}) for e in events) + "\n")


@pytest.fixture
def codex_db(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    db = tmp_path / "state.sqlite"
    monkeypatch.setattr(registry, "CODEX_STATE", db)
    return db


def test_codex_rollout_cache_hit_invalidate_and_evict(codex_db: Path, tmp_path: Path, reads: _Counter) -> None:
    r1, r2 = tmp_path / "r1.jsonl", tmp_path / "r2.jsonl"
    _write_rollout(r1, "user_message")
    _write_rollout(r2, "task_complete")
    _write_codex_db(codex_db, [("a", "/w/a", r1), ("b", "/w/b", r2)])

    registry._codex_threads([("/w/a", "ttys001"), ("/w/b", "ttys002")])
    registry._codex_threads([("/w/a", "ttys001"), ("/w/b", "ttys002")])
    assert sorted(reads.tails) == [r1, r2], "第二輪同 mtime/size 不重讀"

    now = time.time()
    _write_rollout(r1, "user_message", "task_complete")
    os.utime(r1, (now + 5, now + 5))
    kinds = {s.session_id: s._tail_kind for s in registry._codex_threads([("/w/a", "t"), ("/w/b", "t")])}
    assert kinds == {"codex:a": "waiting", "codex:b": "waiting"}
    assert reads.tails.count(r1) == 2
    assert reads.tails.count(r2) == 1

    con = sqlite3.connect(codex_db)
    con.execute("update threads set archived = 1 where id = 'b'")
    con.commit()
    con.close()
    registry._codex_threads([("/w/a", "t")])
    assert len(registry.CODEX_ROLLOUT_CACHE) == 1, "已不在本輪的 rollout 要被淘汰"
