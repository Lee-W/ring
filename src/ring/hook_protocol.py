"""Provider-neutral hook event normalization for RiNG.

外部工具只要能把事件 JSON 餵給 ``ring hook``，就能寫入同一份 registry。
provider adapter 負責把各工具的事件名稱與欄位正規化，registry writer 不需要知道
Claude Code、Codex 或未來工具的細節。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from ring.registry import Status


class HookAdapter(Protocol):
    """把某個 provider 的 hook payload 正規化成 RiNG event。"""

    provider: str
    process_names: tuple[str, ...]

    def normalize(self, data: Mapping[str, Any]) -> NormalizedHookEvent | None:
        """回傳正規化事件；不支援或 payload 不足時回 ``None``。"""


@dataclass(frozen=True)
class NormalizedHookEvent:
    provider: str
    event: str
    session_id: str
    cwd: str
    status: Status
    transcript_path: str = ""
    tty: str = ""
    last_action: str = ""


_COMMON_EVENT_STATUS = {
    "SessionStart": Status.WORKING,
    "UserPromptSubmit": Status.WORKING,
    "Notification": Status.WAITING,
    "Stop": Status.WAITING,
    "SessionEnd": Status.ENDED,
}


class CommonHookAdapter:
    """共用事件語意 adapter。

    適合 Claude Code、Codex，以及未來沿用類似 lifecycle event 的 agent CLI。
    """

    def __init__(self, provider: str, process_names: tuple[str, ...]) -> None:
        self.provider = provider
        self.process_names = process_names

    def normalize(self, data: Mapping[str, Any]) -> NormalizedHookEvent | None:
        event = _first_str(data, "event", "event_name", "hook_event_name", "hookEventName")
        sid = _first_str(data, "session_id", "sessionId", "thread_id", "threadId", "id")
        if not event or not sid:
            return None
        status = _COMMON_EVENT_STATUS.get(event)
        if status is None:
            return None
        return NormalizedHookEvent(
            provider=self.provider,
            event=event,
            session_id=_qualified_session_id(self.provider, sid),
            cwd=_first_str(data, "cwd", "current_working_directory", "currentWorkingDirectory"),
            status=status,
            transcript_path=_first_str(data, "transcript_path", "transcriptPath", "rollout_path", "rolloutPath"),
            tty=_first_str(data, "tty"),
            last_action=_first_str(data, "last_action", "lastAction", "message", "title"),
        )


def adapter_for(provider: str) -> HookAdapter:
    normalized = normalize_provider(provider)
    if normalized in {"claude", "claude-code"}:
        return CommonHookAdapter("claude-code", ("claude",))
    if normalized == "codex":
        return CommonHookAdapter("codex", ("codex",))
    return CommonHookAdapter(normalized or "generic", ())


def provider_from_payload(data: Mapping[str, Any], fallback: str = "claude-code") -> str:
    return normalize_provider(_first_str(data, "provider", "source") or fallback)


def normalize_provider(provider: str) -> str:
    return provider.strip().lower().replace("_", "-")


def _first_str(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _qualified_session_id(provider: str, sid: str) -> str:
    if ":" in sid:
        return sid
    if provider in {"claude", "claude-code"}:
        return sid
    return f"{provider}:{sid}"


HOOK_EVENTS = tuple(_COMMON_EVENT_STATUS)
