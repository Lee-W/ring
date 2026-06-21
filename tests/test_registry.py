from pathlib import Path
from typing import Any

import pytest

from ring.registry import (
    Status,
    _apply_waiting,
    _clean_text,
    _conversation_tail_kind,
    _extract_todo,
    _latest_action,
    _scan_status,
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
