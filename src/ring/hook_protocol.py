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
    waiting_for: str = ""
    detail: str = ""  # 需要你決策的具體內容（要跑的指令 / 問的問題），給 TUI detail 與通知內文


_ALWAYS_STATUS = {
    "SessionStart": Status.WORKING,
    "UserPromptSubmit": Status.WORKING,
    "Stop": Status.IDLE,
    "SessionEnd": Status.ENDED,
    "PermissionRequest": Status.WAITING,
}

_ACTION_REQUIRED_NOTIFICATION_TYPES = {
    "permission_prompt",
    "elicitation_dialog",
}

_ACTION_REQUIRED_WAITING_FOR = {
    "approval",
    "choice",
    "choices",
    "elicitation",
    "input",
    "option",
    "options",
    "permission",
    "question",
    "questions",
    "selection",
    "user",
    "user_input",
}

_IDLE_WAITING_FOR = {
    "",
    "complete",
    "done",
    "idle",
    "instruction",
    "next_step",
    "none",
    "prompt",
    "turn_complete",
    "user_prompt",
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
        status = _ALWAYS_STATUS.get(event)
        explicit_requires_action = _explicit_requires_action(data)
        if event == "SessionEnd":
            status = Status.ENDED
        elif event in {"SessionStart", "UserPromptSubmit"}:
            status = Status.WORKING
        elif explicit_requires_action is not None:
            status = Status.WAITING if explicit_requires_action else Status.IDLE
        elif event == "Notification":
            status = Status.WAITING if _is_action_required_notification(data) else Status.IDLE
        elif event == "PreToolUse":
            # 工具要動了：權限 / 選項類 → 🔴 WAITING；其餘 → 🟢 WORKING。
            # 非 action 的 PreToolUse 也明確收斂成 WORKING（而非不寫），這樣
            # 上一個 WAITING（例如剛答完的權限）會被下一個工具動作清掉。
            status = Status.WAITING if _is_action_required_payload(data) else Status.WORKING
        elif event == "PostToolUse":
            # 工具跑完代表使用者已放行、agent 又在動 → 🟢 WORKING。
            # 這是「回應完」最乾淨的訊號，用來清掉卡住的 WAITING、止住重複通知。
            status = Status.WORKING
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
            waiting_for=_waiting_for(data),
            detail=_action_detail(data),
        )


def adapter_for(provider: str) -> HookAdapter:
    normalized = normalize_provider(provider)
    if normalized in {"claude", "claude-code"}:
        return CommonHookAdapter("claude-code", ("claude",))
    if normalized == "codex":
        return CommonHookAdapter("codex", ("codex",))
    return CommonHookAdapter(normalized or "generic", ())


def provider_from_payload(data: Mapping[str, Any], fallback: str = "claude-code") -> str:
    # 只認明確的 "provider" 欄位。不要拿 "source"——Claude Code 的 SessionStart payload
    # 帶 source="startup"/"resume"/"clear"/"compact"（觸發來源，不是 provider），
    # 誤當 provider 會生出 "startup:<id>" 幽靈 session、且永遠不會被標離場。
    return normalize_provider(_first_str(data, "provider") or fallback)


def normalize_provider(provider: str) -> str:
    return provider.strip().lower().replace("_", "-")


def _first_str(data: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _is_action_required_notification(data: Mapping[str, Any]) -> bool:
    notification_type = _normalize_token(
        _first_str(data, "notification_type", "notificationType", "raw_notification_type", "rawNotificationType")
    )
    if notification_type in _ACTION_REQUIRED_NOTIFICATION_TYPES:
        return True
    return _is_action_required_payload(data)


def _explicit_requires_action(data: Mapping[str, Any]) -> bool | None:
    for key in (
        "requires_action",
        "requiresAction",
        "action_required",
        "actionRequired",
        "needs_user_action",
        "needsUserAction",
        "requires_input",
        "requiresInput",
        "interactive",
    ):
        parsed = _parse_bool(data.get(key))
        if parsed is not None:
            return parsed

    waiting_for = _waiting_for(data)
    if not waiting_for:
        return None
    if waiting_for in _ACTION_REQUIRED_WAITING_FOR:
        return True
    if waiting_for in _IDLE_WAITING_FOR:
        return False
    return None


def _is_action_required_payload(data: Mapping[str, Any]) -> bool:
    tool_name = _first_str(data, "tool_name", "toolName", "tool")
    if tool_name == "AskUserQuestion":
        return True

    tool_input = data.get("tool_input") or data.get("toolInput") or data.get("input")
    if isinstance(tool_input, Mapping):
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions:
            return True
        options = tool_input.get("options")
        if isinstance(options, list) and options:
            return True

    for key in ("questions", "options", "choices"):
        value = data.get(key)
        if isinstance(value, list) and value:
            return True
    return False


# tool_input 裡最能代表「這次要動什麼」的欄位，依序取第一個非空字串。
_DETAIL_INPUT_KEYS = ("command", "file_path", "path", "url", "pattern", "description")

_DETAIL_MAX = 160


def _action_detail(data: Mapping[str, Any]) -> str:
    """從 payload 萃取「到底在等什麼」的具體內容，供 TUI detail 與通知內文。

    優先序：AskUserQuestion 的第一個問題 > tool_input 的代表性欄位（command /
    file_path…）> Notification 的 message。有 tool 名時加前綴（``Bash: rm -rf …``）。
    壓成單行、上限 ``_DETAIL_MAX`` 字元；什麼都沒有回空字串。
    """
    tool = _first_str(data, "tool_name", "toolName", "tool")
    tool_input = data.get("tool_input") or data.get("toolInput") or data.get("input")

    detail = ""
    if isinstance(tool_input, Mapping):
        questions = tool_input.get("questions")
        if isinstance(questions, list) and questions:
            q0 = questions[0]
            if isinstance(q0, Mapping):
                detail = str(q0.get("question") or "")
            elif isinstance(q0, str):
                detail = q0
        if not detail:
            for key in _DETAIL_INPUT_KEYS:
                value = tool_input.get(key)
                if isinstance(value, str) and value:
                    detail = value
                    break
    if not detail:
        detail = _first_str(data, "message", "prompt", "question")

    if tool and detail:
        out = f"{tool}: {detail}"
    else:
        out = tool or detail
    out = " ".join(out.split())  # 多行指令壓成單行
    return out if len(out) <= _DETAIL_MAX else out[: _DETAIL_MAX - 1] + "…"


def _waiting_for(data: Mapping[str, Any]) -> str:
    return _normalize_token(
        _first_str(
            data,
            "waiting_for",
            "waitingFor",
            "requires",
            "reason",
            "interaction",
            "interaction_type",
            "interactionType",
        )
    ).replace("-", "_")


def _parse_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "y", "on"}:
            return True
        if normalized in {"0", "false", "no", "n", "off"}:
            return False
    return None


def _normalize_token(value: str) -> str:
    return value.strip().lower()


def _qualified_session_id(provider: str, sid: str) -> str:
    if ":" in sid:
        return sid
    if provider in {"claude", "claude-code"}:
        return sid
    return f"{provider}:{sid}"


HOOK_EVENTS = (
    "SessionStart",
    "UserPromptSubmit",
    "Notification",
    "PermissionRequest",
    "PreToolUse",
    "PostToolUse",
    "Stop",
    "SessionEnd",
)
