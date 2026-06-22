import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import ring.registry as registry
from ring.registry import Session, Status, _head_cwd
from ring.sources import discover_sessions, get_by_id


def _write_session(
    projects: Path,
    project_enc: str,
    sid: str,
    cwd: str,
    mtime: float,
    extra_cwds: list[str] | None = None,
) -> None:
    """在 ``projects/project_enc/sid.jsonl`` 寫入測試 session 檔案。

    :param projects:    projects 根目錄（tmp_path 下）。
    :param project_enc: Claude 的目錄名編碼（例如 ``"-work-app"``）。
    :param sid:         session id（不含 ``.jsonl``）。
    :param cwd:         第一筆紀錄的 cwd（開場 cwd）。
    :param mtime:       設定 mtime（模擬最後活躍時間）。
    :param extra_cwds:  若提供，依序在第一筆後追加帶有不同 cwd 的紀錄（模擬中途 cd）。
                        最後一筆 cwd 即為「當下 cwd」。既有單筆呼叫不受影響。
    """
    d = projects / project_enc
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"{sid}.jsonl"
    lines = [
        json.dumps({"type": "assistant", "cwd": cwd, "message": {"content": [{"type": "tool_use", "name": "Edit"}]}})
    ]
    for extra_cwd in extra_cwds or []:
        lines.append(
            json.dumps(
                {"type": "assistant", "cwd": extra_cwd, "message": {"content": [{"type": "tool_use", "name": "Bash"}]}}
            )
        )
    f.write_text("\n".join(lines) + "\n")
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


def test_discover_synthetic_row_for_live_proc_without_jsonl(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


def test_discover_no_synthetic_row_when_scan_covers_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
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


# ---------------------------------------------------------------------------
# origin_cwd 修復：中途 cd 漂移（bug fix 核心場景）
# ---------------------------------------------------------------------------


def test_scan_attributes_by_origin_cwd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """中途 cd 的 session：project 應歸屬開場 cwd（maigo），當下 cwd 仍指目的地（mujica）。

    本測試覆蓋 bug 的核心場景：session 在 maigo 開場、中途 cd 到 mujica。
    修復前 project == "mujica"（漂移）；修復後 project == "maigo"（正確）。
    """
    projects = tmp_path / "projects"
    now = time.time()
    maigo = str(tmp_path / "maigo")
    mujica = str(tmp_path / "mujica")
    # 第一筆 cwd=maigo（開場），最後一筆 cwd=mujica（中途 cd 後）
    _write_session(projects, "-maigo", "cd-session", maigo, now, extra_cwds=[mujica])
    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")
    monkeypatch.setattr(registry, "_claude_procs", lambda: [(mujica, "")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    sessions = discover_sessions()
    assert len(sessions) == 1
    s = sessions[0]
    assert s.project == "maigo", f"project 應為 maigo（開場），實際 {s.project!r}"
    assert s.cwd == mujica, f"當下 cwd 應為 mujica，實際 {s.cwd!r}"
    assert s.origin_cwd == maigo, f"origin_cwd 應為 maigo，實際 {s.origin_cwd!r}"


def test_head_cwd(tmp_path: Path) -> None:
    """_head_cwd 從檔頭找首個非空 cwd，跳過無 cwd 的 meta 筆；空檔或無 cwd 回 ''。"""
    # (a) 檔頭數筆無 cwd meta，之後才出現 cwd → 回第一個 cwd
    jsonl = tmp_path / "s.jsonl"
    meta_no_cwd = json.dumps({"type": "last-prompt", "prompt": "do X"})
    meta_mode = json.dumps({"type": "mode", "value": "normal"})
    first_cwd_record = json.dumps({"type": "assistant", "cwd": "/work/maigo", "message": {}})
    later_cwd_record = json.dumps({"type": "assistant", "cwd": "/work/mujica", "message": {}})
    jsonl.write_text("\n".join([meta_no_cwd, meta_mode, first_cwd_record, later_cwd_record]) + "\n")
    assert _head_cwd(jsonl) == "/work/maigo"

    # (b) 空檔 → 回 ""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert _head_cwd(empty) == ""

    # (c) 全無 cwd 欄位的紀錄 → 回 ""
    no_cwd = tmp_path / "nocwd.jsonl"
    no_cwd.write_text(json.dumps({"type": "system", "note": "hi"}) + "\n")
    assert _head_cwd(no_cwd) == ""


def test_scan_no_phantom_synthetic_on_cd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """maigo 案：scan row origin=maigo/當下=mujica，proc 當下=mujica → 不應產生 mujica synthetic 列。

    這驗證 _synthetic_sessions 的差集用的是「當下 cwd」（s.cwd），
    而非 origin_cwd，才不會誤判 mujica 為「無 row」而多生一列。
    """
    projects = tmp_path / "projects"
    now = time.time()
    maigo = str(tmp_path / "maigo")
    mujica = str(tmp_path / "mujica")
    _write_session(projects, "-maigo", "cd-session", maigo, now, extra_cwds=[mujica])
    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")
    monkeypatch.setattr(registry, "_claude_procs", lambda: [(mujica, "/dev/ttys5")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    sessions = discover_sessions()
    # 不應該有任何 source="proc" 的 mujica 列（因為 scan 列當下 cwd 已是 mujica）
    phantom = [s for s in sessions if s.source == "proc" and s.cwd == mujica]
    assert phantom == [], f"不應有 mujica synthetic 列，但出現了：{phantom}"
    # 應該只有一列（scan 列）
    assert len(sessions) == 1
    assert sessions[0].source == "scan"


def test_scan_multi_session_same_origin_after_cd(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """同 origin、其一已 cd 到別處時，兩個 session 各自的當下 cwd 都有 live proc → 兩個都不是 ENDED。

    場景：
    - session A（較新）：origin=maigo，已 cd 到 mujica（當下 cwd=mujica）
    - session B（較舊）：origin=maigo，仍在 maigo（當下 cwd=maigo）
    - live proc：maigo 有一個、mujica 有一個

    改回 by_cwd 分組前的回歸：A 與 B 落在同一 origin 群組，B 在組內排第 2（i=1），
    但 live_n 用 mujica（分組鍵，錯誤）查到 1，i=1 >= live_n=1 → B 被打成 ENDED。
    改回 by_cwd 分組後，A 在 mujica 組、B 在 maigo 組，各自 i=0 < live_n=1 → 都活著。

    另外驗證 session A 的 project 仍歸屬 origin（maigo），而非當下 cwd（mujica）。
    """
    projects = tmp_path / "projects"
    now = time.time()
    maigo = str(tmp_path / "maigo")
    mujica = str(tmp_path / "mujica")

    # session A：較新（now-200），origin=maigo，cd 到 mujica
    _write_session(projects, "-maigo", "sess-a", maigo, now - 200, extra_cwds=[mujica])
    # session B：較舊（now-300），origin=maigo，仍在 maigo
    _write_session(projects, "-maigo", "sess-b", maigo, now - 300)

    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")
    # maigo 有一個 proc、mujica 有一個 proc
    monkeypatch.setattr(registry, "_claude_procs", lambda: [(maigo, "/dev/ttys1"), (mujica, "/dev/ttys2")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    by_id = {s.session_id: s for s in discover_sessions()}

    # 兩個都不是 ENDED——各自的當下 cwd 都有 live proc
    assert by_id["sess-a"].status is not Status.ENDED, "sess-a（cd 到 mujica）應為 alive，不該 ENDED"
    assert by_id["sess-b"].status is not Status.ENDED, "sess-b（仍在 maigo）應為 alive，不該 ENDED"

    # sess-a 的 project 歸屬 origin（maigo），不是當下 cwd（mujica）
    assert by_id["sess-a"].project == "maigo", f"project 應為 maigo，實際 {by_id['sess-a'].project!r}"


def test_scan_commitizen_regression(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """commitizen 案回歸：開場 cwd == proc cwd（無 cd），session 歸屬正確且不多生列。

    commitizen 開場直接在 /work/commitizen 啟動 Claude，無中途 cd。
    此時 origin_cwd == cwd == proc cwd，歸屬與活躍判定應與修復前完全相同。
    """
    projects = tmp_path / "projects"
    now = time.time()
    cwd = str(tmp_path / "commitizen")
    _write_session(projects, "-commitizen", "cz-session", cwd, now)
    monkeypatch.setattr(registry, "CLAUDE_PROJECTS", projects)
    monkeypatch.setattr(registry, "RING_REGISTRY", tmp_path / "noreg")
    monkeypatch.setattr(registry, "_claude_procs", lambda: [(cwd, "/dev/ttys7")])
    monkeypatch.setattr(registry, "_tmux_targets", lambda: {})

    sessions = discover_sessions()
    assert len(sessions) == 1, f"只應有一列，實際 {len(sessions)} 列"
    s = sessions[0]
    assert s.project == "commitizen"
    assert s.cwd == cwd
    assert s.origin_cwd == cwd  # 無 cd，origin == 當下
    assert s.source == "scan"
    # proc 有活，應標為 WORKING（剛建立，idle < threshold）
    assert s.status is Status.WORKING


# ---------------------------------------------------------------------------
# get_by_id
# ---------------------------------------------------------------------------


def test_get_by_id_returns_session_when_found() -> None:
    """get_by_id 對存在的 uuid 回對應 Session，且每次呼叫都重跑 discover。"""
    session_a = Session("uuid-a", "/x/a", Status.WAITING, 0.0, "→ Edit", "hook")
    session_b = Session("uuid-b", "/x/b", Status.WORKING, 0.0, "→ Bash", "hook")
    call_count = 0

    def fake_discover() -> list[Session]:
        nonlocal call_count
        call_count += 1
        return [session_a, session_b]

    with patch("ring.sources.discover_sessions", fake_discover):
        result = get_by_id("uuid-a")

    assert result is session_a
    assert call_count == 1


def test_get_by_id_returns_none_when_not_found() -> None:
    """get_by_id 對不存在的 uuid 回 None。"""
    session_a = Session("uuid-a", "/x/a", Status.WAITING, 0.0, "→ Edit", "hook")

    with patch("ring.sources.discover_sessions", return_value=[session_a]):
        result = get_by_id("nonexistent-uuid")

    assert result is None


def test_get_by_id_reruns_discover_each_call() -> None:
    """每次呼叫 get_by_id 都重跑 discover_sessions（不快取）。"""
    call_count = 0

    def fake_discover() -> list[Session]:
        nonlocal call_count
        call_count += 1
        return []

    with patch("ring.sources.discover_sessions", fake_discover):
        get_by_id("uuid-x")
        get_by_id("uuid-x")

    assert call_count == 2, "每次呼叫 get_by_id 都應重跑 discover_sessions"
