from pathlib import Path

from ring.registry import (
    Status,
    _clean_text,
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
