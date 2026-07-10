"""Session 來源 package——讓 RiNG 不綁死 Claude Code。

每個工具是一個 ``SessionSource``：core 不認識任何具體工具，只把各 source 吐出的
``Session`` 收齊、配 tmux 座標、排序。要支援別的 agent CLI＝在這個 package 加一個
模組、``register_source()`` 註冊，core 零改動（跟 ``focus`` 的 focuser 同一套設計）。
"""

from __future__ import annotations

import ring.registry as registry
from ring.registry import Session, Status
from ring.sources.base import SessionSource
from ring.sources.claude_code import source as _claude_code
from ring.sources.codex import source as _codex
from ring.sources.hook_registry import source as _hook_registry

# 註冊表（順序＝彙整順序）。hook registry 先於 zero-config source，精準事件優先。
_SOURCES: list[SessionSource] = [_hook_registry, _claude_code, _codex]


def register_source(source: SessionSource, *, first: bool = False) -> None:
    """外部擴充入口：註冊一個自訂 session 來源。"""
    if first:
        _SOURCES.insert(0, source)
    else:
        _SOURCES.append(source)


def sources() -> list[SessionSource]:
    """目前已註冊的來源。"""
    return list(_SOURCES)


def discover_sessions() -> list[Session]:
    """場館點名：彙整所有來源的 session，配 tmux 座標、排序（等你的排最上面）。

    手動隱藏（``dd``）的 session 預設不收進看板；但只要它在任一來源出現的
    ``last_active`` 比隱藏當下還新，就代表它又活了——自動解除隱藏並照樣收進來，
    這對所有來源都成立，不只裝了 hook 的 session。
    """
    merged: dict[str, Session] = {}
    for source in _SOURCES:
        for s in source.discover():
            current = merged.get(s.session_id)
            merged[s.session_id] = s if current is None else _merge_duplicate_session(current, s)

    hidden = registry.hidden_sessions()
    found: list[Session] = []
    for s in merged.values():
        hidden_at = hidden.get(s.session_id)
        if hidden_at is None:
            found.append(s)
            continue
        if s.last_active > hidden_at:
            registry.unhide_session(s.session_id)
            found.append(s)
        # 否則：仍在隱藏保留期內、沒有新活動 → 不收進看板。

    bound_targets = registry._tmux_pane_targets()
    process_targets = registry._tmux_process_tree_targets(found)
    targets = registry._tmux_targets()
    targets_by_cwd = registry._tmux_targets_by_cwd()
    used_by_cwd: dict[str, int] = {}
    for s in found:
        if s.tmux_pane:
            if bound := bound_targets.get(s.tmux_pane):
                s.tmux_target = bound
                continue

        if process_target := process_targets.get(s.session_id):
            s.tmux_target = process_target
            continue

        cwd_targets = targets_by_cwd.get(s.cwd, [])
        if len(cwd_targets) > 1:
            idx = used_by_cwd.get(s.cwd, 0)
            if idx < len(cwd_targets):
                s.tmux_target = cwd_targets[idx]
                used_by_cwd[s.cwd] = idx + 1
                continue

        s.tmux_target = targets.get(s.cwd)
    found.sort(key=lambda s: (s.status.rank, s.idle_for))
    return found


def _merge_duplicate_session(current: Session, candidate: Session) -> Session:
    """同一 session 來自多個來源時合併。

    hook registry 通常最精準，所以預設保留先到的 hook row。不過 Claude Code 有些
    action-required 狀態之後未必會送出能清掉 waiting 的 hook；如果 zero-config scan
    已看到同一 session 有更新紀錄且不再是 WAITING，就用 scan 清掉 stale waiting，
    同時保留 hook 拿到的 tty，避免跳轉能力退化。

    只看 last_active 先後還不夠：Claude Code 在送出需要權限的 tool_use 之後，會繼續
    往同一份 transcript 追加幾筆非對話簿記紀錄（last-prompt / ai-title / mode /
    permission-mode 等），這些紀錄一樣會推進檔案 mtime，但不代表使用者真的回應了
    權限請求。因此還要求 candidate 的 ``_tail_kind`` 不是 ``"interrupted"``——
    只有真人 prompt（"working"）或對話真的收尾（"waiting"）才算「使用者真的回應了」，
    單純簿記寫入（tail 仍是 interrupted）不該觸發覆蓋。
    """
    if (
        current.source == "hook"
        and current.status is Status.WAITING
        and candidate.provider == current.provider
        and candidate.status is not Status.WAITING
        and candidate.last_active > current.last_active
        and candidate._tail_kind != "interrupted"
    ):
        if not candidate.tty:
            candidate.tty = current.tty
        if not candidate.tmux_target:
            candidate.tmux_target = current.tmux_target
        return candidate
    return current


def get_by_id(session_id: str) -> Session | None:
    """現查現給：重跑 discover_sessions() 後 filter 出指定 session_id。

    每次呼叫都重跑 discover，不快取舊 Session——scan 的 tty 只在「該 cwd 剛好一個
    live claude」才填，舊 Session 的 tty 可能已失效（例如點擊通知時），
    必須重新 discover 才能拿到當下有效的 tty。

    :param session_id: 要查詢的 session id。
    :returns: 找到時回對應 Session；找不到回 None。
    """
    for s in discover_sessions():
        if s.session_id == session_id:
            return s
    return None


__all__ = [
    "SessionSource",
    "discover_sessions",
    "get_by_id",
    "register_source",
    "sources",
]
