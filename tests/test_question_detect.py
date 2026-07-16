from pathlib import Path

from ring.question_detect import (
    _strip_trailing_code_fence,
    stop_last_assistant_text,
    trailing_question_detail,
)


def test_trailing_question_detected() -> None:
    text = "先做了 A。\n\n要不要順便修 B？"
    assert trailing_question_detail(text) == "要不要順便修 B？"


def test_trailing_statement_not_flagged() -> None:
    text = "先確認一下：你要 A 還是 B？我會用 A 繼續做。"
    assert trailing_question_detail(text) == ""


def test_question_mid_message_statement_at_end_not_flagged() -> None:
    text = "要用哪個方案？我選了方案一，正在套用。"
    assert trailing_question_detail(text) == ""


def test_empty_text_not_flagged() -> None:
    assert trailing_question_detail("") == ""


def test_trailing_code_fence_stripped_before_check() -> None:
    text = "這樣可以嗎？\n\n```bash\nls -la\n```"
    assert trailing_question_detail(text) == "這樣可以嗎？"


def test_trailing_code_fence_with_statement_still_not_flagged() -> None:
    text = "我先跑一下這個指令。\n\n```bash\nls -la\n```"
    assert trailing_question_detail(text) == ""


def test_ascii_question_mark_detected() -> None:
    assert trailing_question_detail("Should I proceed with the deploy?") == "Should I proceed with the deploy?"


def test_trailing_wrapper_chars_stripped() -> None:
    assert trailing_question_detail("要繼續嗎？**") == "要繼續嗎？"


def test_detail_truncated_when_too_long() -> None:
    long_line = "要不要" + "很長" * 100 + "？"
    detail = trailing_question_detail(long_line)
    assert len(detail) == 160
    assert detail.endswith("…")


def test_strip_trailing_code_fence_removes_multiple_blocks() -> None:
    text = "問句？\n```\na\n```\n\n```\nb\n```"
    assert _strip_trailing_code_fence(text) == "問句？"


def test_stop_last_assistant_text_prefers_payload_field() -> None:
    data = {"last_assistant_message": "要繼續嗎？", "transcript_path": "/does/not/exist.jsonl"}
    assert stop_last_assistant_text(data) == "要繼續嗎？"


def test_stop_last_assistant_text_falls_back_to_transcript(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "要繼續嗎？"}]}}\n',
        encoding="utf-8",
    )
    data = {"transcript_path": str(transcript)}
    assert stop_last_assistant_text(data) == "要繼續嗎？"


def test_stop_last_assistant_text_missing_transcript_returns_empty(tmp_path: Path) -> None:
    data = {"transcript_path": str(tmp_path / "missing.jsonl")}
    assert stop_last_assistant_text(data) == ""


def test_stop_last_assistant_text_malformed_transcript_returns_empty(tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("not json at all\n", encoding="utf-8")
    data = {"transcript_path": str(transcript)}
    assert stop_last_assistant_text(data) == ""


def test_stop_last_assistant_text_no_fields_returns_empty() -> None:
    assert stop_last_assistant_text({}) == ""
