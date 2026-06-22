import json
from pathlib import Path
from typing import Any

import pytest

from ring.registry import (
    Session,
    Status,
    _apply_waiting,
    _clean_text,
    _conversation_tail_kind,
    _extract_todo,
    _hook_sessions,
    _latest_action,
    _scan_status,
    _synthetic_sessions,
    _tail_records,
    _tool_summary,
)


def test_status_rank_ordering() -> None:
    assert Status.WAITING.rank < Status.WORKING.rank < Status.IDLE.rank < Status.ENDED.rank


def test_scan_status_thresholds() -> None:
    assert _scan_status(10) is Status.WORKING
    assert _scan_status(1000) is Status.IDLE
    assert _scan_status(10**9) is Status.ENDED


def test_clean_text_strips_command_noise() -> None:
    assert _clean_text("<local-command-stdout>Bye!</local-command-stdout>") == ""
    assert _clean_text("  hello\nworld ") == "hello"  # 只取第一行


def test_tool_summary_enriches_common_tools() -> None:
    assert _tool_summary({"name": "Edit", "input": {"file_path": "/a/b/foo.py"}}) == "→ Edit foo.py"
    assert _tool_summary({"name": "Bash", "input": {"command": "git status\n"}}) == "→ Bash: git status"
    assert _tool_summary({"name": "Grep", "input": {"pattern": "TODO"}}) == "→ Grep TODO"
    assert _tool_summary({"name": "WebFetch"}) == "→ WebFetch"  # 沒特例 → 退回工具名
    assert _tool_summary({}) == ""


def test_latest_action_prefers_tool_use_and_skips_noise() -> None:
    records = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "<local-command-stdout>x</local-command-stdout>"}]},
        },
    ]
    assert _latest_action(records) == "→ Edit"


def test_latest_action_falls_back_to_assistant_text() -> None:
    records = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hello there"}]}}]
    assert _latest_action(records) == "hello there"


def test_extract_todo_counts_completed() -> None:
    records = [
        {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {"todos": [{"status": "completed"}, {"status": "completed"}, {"status": "pending"}]},
                    }
                ]
            }
        }
    ]
    assert _extract_todo(records) == (2, 3)


def test_extract_todo_none_when_absent() -> None:
    records = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    assert _extract_todo(records) is None


def test_tail_records_reads_last_valid_json(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(['{"a": 1}', "not json", '{"b": 2}']) + "\n")
    records = _tail_records(p)
    assert records[-1] == {"b": 2}
    assert {"a": 1} in records


# ---------------------------------------------------------------------------
# _conversation_tail_kind (Test plan A)
# ---------------------------------------------------------------------------

_END_TURN_RECORD = {
    "type": "assistant",
    "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
}

_TOOL_USE_RECORD = {
    "type": "assistant",
    "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]},
}

_USER_TOOL_USE_RESULT_FIELD = {
    "type": "user",
    "toolUseResult": {"output": "ok"},
    "message": {"content": []},
}

_USER_TOOL_RESULT_BLOCK = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
}

_USER_REAL_PROMPT = {
    "type": "user",
    "message": {"content": [{"type": "text", "text": "do X"}]},
}


@pytest.mark.parametrize(
    "records,expected",
    [
        ([_END_TURN_RECORD], "waiting"),
        ([_TOOL_USE_RECORD], "interrupted"),
        ([_USER_TOOL_USE_RESULT_FIELD], "interrupted"),
        ([_USER_TOOL_RESULT_BLOCK], "interrupted"),
        ([_USER_REAL_PROMPT], "working"),
    ],
    ids=[
        "end_turn_is_waiting",
        "tool_use_is_interrupted",
        "user_toolUseResult_field_is_interrupted",
        "user_tool_result_block_is_interrupted",
        "real_prompt_is_working",
    ],
)
def test_tail_kind_basic(records: list[dict[str, Any]], expected: str) -> None:
    assert _conversation_tail_kind(records) == expected


def test_tail_kind_skips_noise_to_find_end_turn() -> None:
    """尾巴全是「跳過型」噪音，仍要往回走到 end_turn → waiting。
    注意：尾巴只放 type 非 user/assistant、isMeta、command 噪音——不混入 tool_result（那是 interrupted）。
    """
    noise_type: dict[str, Any] = {"type": "file-history-snapshot", "cwd": "/foo"}
    noise_system: dict[str, Any] = {"type": "system"}
    noise_meta: dict[str, Any] = {
        "type": "user",
        "isMeta": True,
        "message": {"content": [{"type": "text", "text": "meta"}]},
    }
    noise_cmd: dict[str, Any] = {
        "type": "user",
        "message": {"content": [{"type": "text", "text": "<local-command-stdout>x</local-command-stdout>"}]},
    }
    records: list[dict[str, Any]] = [_END_TURN_RECORD, noise_type, noise_system, noise_meta, noise_cmd]
    assert _conversation_tail_kind(records) == "waiting"


def test_tail_kind_filters_imeta() -> None:
    """isMeta user 要濾：後接 end_turn → 跳過 isMeta → waiting。"""
    imeta: dict[str, Any] = {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "x"}]}}
    records: list[dict[str, Any]] = [_END_TURN_RECORD, imeta]
    assert _conversation_tail_kind(records) == "waiting"


def test_tail_kind_filters_isidechain() -> None:
    """isSidechain 要濾：sub-agent 旁支不影響主對話判定。"""
    sidechain: dict[str, Any] = {
        "type": "assistant",
        "isSidechain": True,
        "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]},
    }
    records: list[dict[str, Any]] = [_END_TURN_RECORD, sidechain]
    assert _conversation_tail_kind(records) == "waiting"


def test_tail_kind_empty_records_is_none() -> None:
    assert _conversation_tail_kind([]) == "none"


def test_tail_kind_only_noise_records_is_none() -> None:
    records = [{"type": "system"}, {"type": "file-history-snapshot"}]
    assert _conversation_tail_kind(records) == "none"


# ---------------------------------------------------------------------------
# _apply_waiting (Test plan B)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,age,tail_kind,window,expected",
    [
        (Status.IDLE, 600, "waiting", 1800, Status.WAITING),
        (Status.IDLE, 3600, "waiting", 1800, Status.IDLE),
        (Status.IDLE, 600, "interrupted", 1800, Status.IDLE),
        (Status.IDLE, 600, "none", 1800, Status.IDLE),
        (Status.WORKING, 10, "waiting", 1800, Status.WORKING),
        (Status.ENDED, 100, "waiting", 1800, Status.ENDED),
    ],
    ids=[
        "idle_within_window_upgrades_to_waiting",
        "idle_beyond_window_stays_idle",
        "interrupted_tail_stays_idle",
        "none_tail_stays_idle",
        "working_status_not_upgraded",
        "ended_status_not_upgraded",
    ],
)
def test_apply_waiting(status: Status, age: int, tail_kind: str, window: int, expected: Status) -> None:
    assert _apply_waiting(status, age, tail_kind, window) is expected


# ---------------------------------------------------------------------------
# _synthetic_sessions (Test plan A)
# ---------------------------------------------------------------------------


def _make_session(cwd: str) -> Session:
    """建立最小 Session，只填 cwd，其餘用預設值。"""
    return Session(
        session_id=f"scan:{cwd}",
        cwd=cwd,
        status=Status.IDLE,
        last_active=0.0,
        last_action="—",
        source="scan",
    )


def test_synthetic_sessions_basic_one_row() -> None:
    """procs 有一個 cwd、existing 空 → 補一列，各欄位正確。"""
    result = _synthetic_sessions([("/a", "/dev/ttys1")], [])
    assert len(result) == 1
    s = result[0]
    assert s.cwd == "/a"
    assert s.status is Status.IDLE
    assert s.source == "proc"
    assert s.tty == "/dev/ttys1"
    assert s.session_id == "synthetic:/a"


def test_synthetic_sessions_skips_existing_cwd() -> None:
    """existing 已含 cwd=/a 的 Session → 回 []，不補。"""
    existing = [_make_session("/a")]
    result = _synthetic_sessions([("/a", "/dev/ttys1")], existing)
    assert result == []


def test_synthetic_sessions_dedup_same_cwd_takes_first_nonnull_tty() -> None:
    """同 cwd 兩筆 procs（第一筆 tty 空、第二筆有 tty）→ 只回一列，tty 取第一個非空。"""
    result = _synthetic_sessions([("/b", ""), ("/b", "/dev/ttys2")], [])
    assert len(result) == 1
    assert result[0].cwd == "/b"
    assert result[0].tty == "/dev/ttys2"


def test_synthetic_sessions_skips_empty_cwd() -> None:
    """空 cwd 的 proc → 跳過，不生列。"""
    result = _synthetic_sessions([("", "")], [])
    assert result == []


@pytest.mark.parametrize(
    "procs,n_existing_cwds,expected_len",
    [
        ([("/x", ""), ("/y", "/dev/ttys3")], 0, 2),  # 兩個不同 cwd，都不在 existing → 補兩列
        ([("/x", ""), ("/y", "/dev/ttys3")], 1, 1),  # 第一個 cwd 在 existing → 只補一列
        ([("/x", ""), ("", "")], 0, 1),               # 空 cwd 跳過 → 只補 /x
    ],
    ids=["two_cwds_both_new", "two_cwds_one_existing", "one_valid_one_empty"],
)
def test_synthetic_sessions_count(
    procs: list[tuple[str, str]], n_existing_cwds: int, expected_len: int
) -> None:
    cwds = [cwd for cwd, _ in procs if cwd][:n_existing_cwds]
    existing = [_make_session(c) for c in cwds]
    result = _synthetic_sessions(procs, existing)
    assert len(result) == expected_len


# ---------------------------------------------------------------------------
# _hook_sessions stale row cleanup
# ---------------------------------------------------------------------------


def _write_hook_session(registry_dir: Path, sid: str, cwd: str, tty: str) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / f"{sid}.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "cwd": cwd,
                "status": "waiting",
                "last_active": 123.0,
                "last_action": "—",
                "tty": tty,
            }
        )
    )


def test_hook_sessions_ends_stale_tty_even_when_same_cwd_has_live_proc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同 cwd 仍有 live claude 時，舊 hook row 也要用 tty 判斷是否已離場。

    使用者關掉一個 session，但同專案另有 session 還活著時，若只用 cwd 判斷，
    舊 row 會繼續顯示且跳轉失敗。hook 有 tty 時應以 tty 精準排除 stale row。
    """
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "stale", "/work/app", "/dev/ttys001")
    _write_hook_session(registry_dir, "live", "/work/app", "/dev/ttys002")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    by_id = {s.session_id: s for s in _hook_sessions([("/work/app", "/dev/ttys002")])}

    assert by_id["stale"].status is Status.ENDED
    assert by_id["live"].status is Status.WAITING


def test_hook_sessions_keeps_cwd_fallback_when_live_tty_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """live proc 有 cwd 但 tty 取不到時，保留既有 cwd fallback，避免誤殺。"""
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "maybe-live", "/work/app", "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    sessions = _hook_sessions([("/work/app", "")])

    assert sessions[0].status is Status.WAITING
