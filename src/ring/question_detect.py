"""偵測 Stop 事件裡「回合結尾純文字提問」。

agent 有時不用 ``AskUserQuestion``、不觸發權限請求，只是在文字裡問了一句話就停下來
（例如「要不要順便修 B？」）。既有的 Stop → 🟡 跑完停著是寫死的（見
``hook_protocol._ALWAYS_STATUS``），從不讀 assistant 文字，這類提問因此測不到、使用者
以為沒東西要回。

設計原則：保守，寧可漏報也不要誤報。只看**訊息結尾**：

- 問句出現在訊息中段、結尾是陳述句 → 不算（避免把「先確認一下：你要 A 還是 B？我會用
  A 繼續做」這種訊息誤判——它結尾是陳述句，不該轉紅）。
- 結尾若是圍欄程式碼區塊（```` ``` ... ``` ````），先把整個區塊剝掉再看區塊之前的最後一行
  ——常見於「這樣可以嗎？\\n\\n```bash\\nls -la\\n```」，圍欄本身不是問句，但它前面那句是。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ring.transcript import _last_assistant_text, _tail_records

_DETAIL_MAX = 160

# 收尾判斷前，先剝掉這些包裹字元（markdown 強調記號、引號、括號），
# 例如「...要繼續嗎？**」「...要繼續嗎？」」。
_TRAILING_WRAPPERS = "*_`\"' )]>"

_QUESTION_MARKS = ("?", "？")


def stop_last_assistant_text(data: Mapping[str, Any]) -> str:
    """取得 Stop payload 對應的最後一則 assistant 訊息文字。

    優先讀 payload 本身的 ``last_assistant_message``（Claude Code、Codex 的實際 Stop
    payload 都帶這欄——見 ``~/.config/ring/hook_payloads.jsonl`` 實錄）；缺欄位或空字串
    才退回從 ``transcript_path`` 尾端讀（有界讀取，缺檔／壞格式安全回空字串，不炸）。
    """
    text = data.get("last_assistant_message")
    if isinstance(text, str) and text.strip():
        return text
    tp = data.get("transcript_path") or data.get("transcriptPath")
    if isinstance(tp, str) and tp:
        try:
            records = _tail_records(Path(tp))
        except Exception:
            return ""
        return _last_assistant_text(records)
    return ""


def _strip_trailing_code_fence(text: str) -> str:
    """剝掉文字尾端的圍欄程式碼區塊（可能不只一個），回傳剝除後的文字。"""
    lines = text.splitlines()
    changed = True
    while changed and lines:
        changed = False
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip().startswith("```"):
            closing = len(lines) - 1
            opening = None
            for i in range(closing - 1, -1, -1):
                if lines[i].strip().startswith("```"):
                    opening = i
                    break
            if opening is not None:
                lines = lines[:opening]
                changed = True
    return "\n".join(lines)


def _strip_trailing_wrappers(line: str) -> str:
    line = line.rstrip()
    while line and line[-1] in _TRAILING_WRAPPERS:
        line = line[:-1].rstrip()
    return line


def trailing_question_detail(text: str) -> str:
    """``text`` 的結尾若是問句，回傳該行摘要（截斷合理長度）；否則回傳空字串。"""
    if not text:
        return ""
    stripped = _strip_trailing_code_fence(text.rstrip())
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if not lines:
        return ""
    last_line = _strip_trailing_wrappers(lines[-1])
    if not last_line or last_line[-1] not in _QUESTION_MARKS:
        return ""
    detail = " ".join(last_line.split())
    return detail if len(detail) <= _DETAIL_MAX else detail[: _DETAIL_MAX - 1] + "…"
