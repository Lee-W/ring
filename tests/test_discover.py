import json
import os
import time
from pathlib import Path

import pytest

import ring.registry as registry
from ring.registry import Status
from ring.sources import discover_sessions


def _write_session(projects: Path, project_enc: str, sid: str, cwd: str, mtime: float) -> None:
    d = projects / project_enc
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    record = {"type": "assistant", "cwd": cwd, "message": {"content": [{"type": "tool_use", "name": "Edit"}]}}
    f.write_text(json.dumps(record) + "\n")
    os.utime(f, (mtime, mtime))


def test_scan_marks_live_newest_and_ends_the_rest(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    now = time.time()
    # 同一個 cwd 兩個 session，但只有一個活著的 claude → 最新的活、舊的離場
    _write_session(projects, "-work-app", "live", "/work/app", now)
    _write_session(projects, "-work-app", "old", "/work/app", now - 1000)
    # 另一個 cwd 完全沒有活著的 claude → 離場
    _write_session(projects, "-work-blog", "blog", "/work/blog", now - 500)

    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")  # 沒有 hook 資料
    monkeypatch.setattr(registry, "_claude_procs", lambda: [("/work/app", "/dev/ttys010")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    by_id = {s.session_id: s for s in discover_sessions()}

    assert by_id["live"].status is Status.WORKING
    assert by_id["live"].tty == "/dev/ttys010"  # cwd 唯一 claude → tty 分得出來
    assert by_id["old"].status is Status.ENDED  # 同 cwd 但較舊、超過 claude 數
    assert by_id["blog"].status is Status.ENDED  # cwd 沒有活著的 claude


def test_scan_action_parsed_from_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    projects = tmp_path / "projects"
    _write_session(projects, "-work-app", "s", "/work/app", time.time())
    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")
    monkeypatch.setattr(registry, "_claude_procs", lambda: [("/work/app", "")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    sessions = discover_sessions()
    assert len(sessions) == 1
    assert sessions[0].last_action == "→ Edit"
    assert sessions[0].project == "app"


# ---------------------------------------------------------------------------
# 合成補列（Test plan B）
# ---------------------------------------------------------------------------


def test_discover_synthetic_row_for_live_proc_without_jsonl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """live proc 有 cwd 但 projects 目錄裡無對應近期 jsonl → 多一列 source="proc"。"""
    projects = tmp_path / "projects"
    projects.mkdir()  # 空目錄，無任何 jsonl
    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")  # 無 hook 資料
    monkeypatch.setattr(registry, "_claude_procs", lambda: [("/live/ghost", "/dev/ttys9")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    sessions = discover_sessions()
    ghost = next((s for s in sessions if s.cwd == "/live/ghost"), None)
    assert ghost is not None, "應補一列 cwd=/live/ghost"
    assert ghost.status is Status.IDLE
    assert ghost.source == "proc"
    assert ghost.tty == "/dev/ttys9"


def test_discover_no_synthetic_row_when_scan_covers_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同一個 cwd 既有近期 jsonl（scan 列）又有 live proc → 只有 scan 那列，無合成列。"""
    projects = tmp_path / "projects"
    now = time.time()
    _write_session(projects, "-work-app", "existing", "/work/app", now)
    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")
    monkeypatch.setattr(registry, "_claude_procs", lambda: [("/work/app", "")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    sessions = discover_sessions()
    app_sessions = [s for s in sessions if s.cwd == "/work/app"]
    assert len(app_sessions) == 1, "同 cwd 不應同時存在 scan 列 + 合成列"
    assert app_sessions[0].source == "scan"  # 以 scan 列為準，無合成列
