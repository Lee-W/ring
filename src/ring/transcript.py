"""Transcript 解析層：把 agent CLI 的 JSONL 對話紀錄拆成 RiNG 要的訊號。

純函式、零外部依賴（只 stdlib），不認識 ``Session`` 也不碰 process / tmux——
那些屬於 ``registry``。把這層獨立出來，``registry`` 才能專注在「抓 session」，
解析邏輯也好單測。Claude Code 與 Codex 的 transcript 都走這裡的共用 helper。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _tail_records(path: Path, max_tail: int = 128 * 1024) -> list[dict[str, Any]]:
    """讀 JSONL 檔尾若干筆合法 JSON 記錄（舊→新），只 seek 檔尾不整檔讀。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_tail:
                f.seek(size - max_tail)
                f.readline()  # 丟掉被切一半的那行
            chunk = f.read()
    except OSError:
        return []
    out = []
    for raw in chunk.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            out.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    return out


def _head_cwd(path: Path, max_head: int = 128 * 1024) -> str:
    """讀 JSONL 檔頭，回傳第一筆含非空 ``cwd`` 欄位的紀錄之 cwd。

    目的：修補 scan 模式「中途 cd 漂移」問題。Claude Code session 中途 ``cd``
    後，新紀錄的 ``cwd`` 已指向目的地目錄，若只看檔尾（``_tail_records``）會
    誤判 session 歸屬。改讀檔頭取「開場 cwd」才能把 session 留在它真正所屬的
    專案列，而非漂到目的地專案。

    跳過 JSONL 裡無 ``cwd`` 的 meta 筆（last-prompt、mode、permission-mode、
    file-history-snapshot 等）；命中即停，不必整檔讀完。

    :param path:     JSONL 檔案路徑。
    :param max_head: 從檔頭最多讀取的位元組數，預設 128 KiB，足以涵蓋早期幾十筆紀錄。
    :returns:        第一筆非空 cwd 字串；找不到（空檔、全無 cwd）回 ``""``。
    """
    try:
        with path.open("rb") as f:
            chunk = f.read(max_head)
    except OSError:
        return ""
    for raw in chunk.splitlines():
        raw = raw.strip()
        if not raw:
            continue
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        cwd = record.get("cwd")
        if cwd and isinstance(cwd, str):
            return cwd
    return ""


def _blocks(record: dict[str, Any]) -> list[Any]:
    msg = record.get("message")
    if isinstance(msg, dict):
        content = msg.get("content")
        if isinstance(content, list):
            return content
        if isinstance(content, str):
            return [{"type": "text", "text": content}]
    return []


_CMD_NOISE = ("<local-command-stdout>", "<command-name>", "<command-message>", "<command-args>")


def _clean_text(s: str) -> str:
    s = s.strip()
    if any(marker in s for marker in _CMD_NOISE):
        return ""  # slash-command 的 stdout / 標記，不是真動作
    return s.splitlines()[0].strip() if s else ""  # 只取第一行，旁白長文不洗版


def _tool_summary(block: dict[str, Any]) -> str:
    """把一個 tool_use 變成簡短摘要，例如 → Edit foo.py / → Bash: git status。"""
    name = str(block.get("name") or "")
    if not name:
        return ""
    inp = block.get("input")
    if not isinstance(inp, dict):
        inp = {}
    if name in ("Edit", "Write", "Read", "NotebookEdit"):
        path = inp.get("file_path") or inp.get("notebook_path")
        if path:
            return f"→ {name} {Path(str(path)).name}"
    elif name == "Bash":
        cmd = str(inp.get("command") or "").strip().replace("\n", " ")
        if cmd:
            return f"→ Bash: {cmd[:40]}"
    elif name in ("Grep", "Glob") and inp.get("pattern"):
        return f"→ {name} {inp['pattern']}"
    return f"→ {name}"


def _latest_action(records: list[dict[str, Any]]) -> str:
    """從新到舊找最近一個「真動作」：agent 的 tool_use 優先，其次 agent 的文字。

    跳過 user 訊息、tool_result、slash-command 輸出這類雜訊。
    """
    for record in reversed(records):
        for block in _blocks(record):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                summary = _tool_summary(block)
                if summary:
                    return summary
        if record.get("type") == "assistant":
            for block in _blocks(record):
                if isinstance(block, dict) and block.get("type") == "text":
                    txt = _clean_text(str(block.get("text", "")))
                    if txt:
                        return txt[:80]
    return "—"


def _last_assistant_text(records: list[dict[str, Any]]) -> str:
    """從新到舊找最後一筆帶文字的 assistant 記錄，回傳其 text block 合併後的完整文字。

    跳過沒有 text block 的 assistant 記錄（例如純 tool_use）；找不到回空字串。
    給 Stop 事件「回合結尾純文字提問」偵測用——跟 ``_latest_action`` 不同，這裡要完整
    文字（不截斷、不退回 tool_use 摘要），才能判斷訊息結尾是不是問句。
    """
    for record in reversed(records):
        if record.get("type") != "assistant":
            continue
        texts = [
            str(b.get("text", ""))
            for b in _blocks(record)
            if isinstance(b, dict) and b.get("type") == "text" and b.get("text")
        ]
        if texts:
            return "\n".join(texts)
    return ""


def _extract_todo(records: list[dict[str, Any]]) -> tuple[int, int] | None:
    """從新到舊找最新的 TodoWrite，回傳 (done, total)。真進度訊號。"""
    for record in reversed(records):
        for block in _blocks(record):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") == "TodoWrite":
                todos = (block.get("input") or {}).get("todos")
                if isinstance(todos, list) and todos:
                    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
                    return done, len(todos)
    return None


def _conversation_tail_kind(records: list[dict[str, Any]]) -> str:
    """從對話尾巴往回走，判定最近一個「真訊息紀錄」的性質。

    回傳值（str）：
    - ``"waiting"``    : 對話最後是 assistant end_turn 收尾，Claude 回完一輪。
    - ``"working"``    : 對話最後是真人送出的 prompt，輪到 Claude 回應。
    - ``"interrupted"`` : 工具呼叫進行中（assistant tool_use / user tool_result），尚未完成一輪。
    - ``"none"``       : 沒有可判定的訊息（空 records、全噪音、無 assistant/user 訊息）。

    演算法：從 ``reversed(records)`` 往回走，跳過噪音，找第一個真訊息紀錄，
    依 type 與 content 判定後立即 return。絕不直接看 records[-1]——尾巴幾乎都是噪音。

    限制（誠實標明）：靠對話尾巴猜。Notification（卡權限等授權流程）在 JSONL 不留痕跡，
    scan 模式測不到——那種需要使用者決策的狀態仍需 hook 模式偵測。
    """
    for record in reversed(records):
        t = record.get("type")
        # --- 跳過噪音 ---
        if t not in ("user", "assistant"):
            continue  # file-history-snapshot / system / permission-mode / mode / last-prompt 等
        if record.get("isMeta") is True:
            continue
        if record.get("isSidechain") is True:
            continue
        # --- 第一個真訊息紀錄，依 type 判定 ---
        if t == "assistant":
            msg = record.get("message")
            stop_reason = msg.get("stop_reason") if isinstance(msg, dict) else None
            # assistant 帶 tool_use block → 工具呼叫進行中
            if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in _blocks(record)):
                return "interrupted"
            if stop_reason == "end_turn":
                return "waiting"
            # tool_use / stop_sequence / None / 其他 → 視為中途被打斷
            return "interrupted"
        # t == "user"
        # user 帶工具結果（toolUseResult 欄位，或 content 含 tool_result block）→ 中途
        if record.get("toolUseResult") is not None:
            return "interrupted"
        if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in _blocks(record)):
            return "interrupted"
        # command 噪音（slash-command stdout）→ 跳過，繼續往回走
        text_blocks = [b for b in _blocks(record) if isinstance(b, dict) and b.get("type") == "text"]
        if text_blocks and all(_clean_text(str(b.get("text", ""))) == "" for b in text_blocks):
            continue
        # 真人 prompt → 輪到 Claude 回應，不是回合結束
        return "working"
    return "none"


def _recent_actions(records: list[dict[str, Any]], n: int = 5) -> list[str]:
    acts = []
    for record in records:
        for block in _blocks(record):
            if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name"):
                acts.append(str(block["name"]))
    return acts[-n:]
