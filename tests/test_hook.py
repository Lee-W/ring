import fcntl
import io
import json
import multiprocessing
import subprocess
from multiprocessing.synchronize import Event as ProcessEvent
from pathlib import Path
from typing import Any

import pytest

import ring.hook as hook
import ring.registry as registry
import ring.sources as sources
from ring.config import Config
from ring.focus import kitty
from ring.hook import _is_ring_hook_command, install_hooks, uninstall_hooks
from ring.registry import Session, Status

real_session_pid = hook._session_pid


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """讓 hook 測試不受機器 config 影響（預設 backend=auto → 不委派 agent-hooks）。

    同時清空 notifier registry：hook 現在會在 WAITING 事件就地發系統通知，darwin 上
    osascript 永遠可用，不擋會在跑測試時噴真實通知。空 registry → _select_notifier 回
    None → notify_waiting no-op。要驗證「有發」的測試自己注入 spy notifier。
    stats 的狀態轉換 log 也導去 tmp，避免測試寫進使用者的 events.jsonl。run_hook 開頭的
    flush_if_due 也要導去 tmp，避免測試碰到機器上真實的 debounce queue / quiet 狀態檔。"""
    monkeypatch.setattr("ring.hook.get_config", lambda: Config())
    monkeypatch.setattr("ring.notify._NOTIFIERS", [])
    monkeypatch.setattr("ring.stats.EVENTS_PATH", tmp_path / "events.jsonl")
    monkeypatch.setattr("ring.notify_queue._QUEUE_PATH", tmp_path / "notify-queue.json")
    monkeypatch.setattr("ring.notify_queue._QUIET_PATH", tmp_path / "quiet")
    monkeypatch.setattr("ring.notify_queue.get_config", lambda: Config())
    monkeypatch.setattr(hook, "_session_pid", lambda _process_names: None)


def _feed(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _settings_with_ring_hook(settings: Path, cmd: str = "ring hook", timeout: int = hook._HOOK_TIMEOUT) -> None:
    """寫一個已有 ring hook 的 settings.json 到 settings 路徑。"""
    data = {
        "hooks": {e: [{"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}] for e in hook._HOOK_EVENTS}
    }
    settings.write_text(json.dumps(data, indent=2))


def test_session_pid_walks_to_provider_process(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.hook.os.getppid", lambda: 300)
    parents = {
        300: (200, "/bin/zsh"),
        200: (100, "/Users/test/.local/bin/claude"),
    }
    monkeypatch.setattr(hook, "_ps_row", parents.get)

    assert real_session_pid(("claude",)) == 200


def test_stop_writes_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"})
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value
    assert data["cwd"] == "/x"


def test_hook_event_writes_tmux_binding_and_process_pids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(hook, "_controlling_tty", lambda: "/dev/ttys042")
    monkeypatch.setattr(hook, "_session_pid", lambda _process_names: 4242)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"})

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["tmux_pane"] == "%42"
    assert data["tty"] == "/dev/ttys042"
    assert isinstance(data["hook_pid"], int)
    assert data["agent_pid"] == 4242


def test_subagent_event_preserves_foreground_agent_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """subagent hook 共用 host session id，不得把背景 process PID 寫成前景 session 身分。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setattr(hook, "_session_pid", lambda _process_names: 111)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PreToolUse", "cwd": "/x"})
    assert hook.run_hook() == 0

    monkeypatch.setattr(hook, "_session_pid", lambda _process_names: 222)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "PostToolUse",
            "cwd": "/x",
            "agent_id": "agent-a",
            "agent_type": "general-purpose",
        },
    )
    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["agent_pid"] == 111


@pytest.fixture
def hook_registry(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    path = tmp_path / "sessions"
    monkeypatch.setattr(hook, "RING_REGISTRY", path)
    monkeypatch.setattr(hook, "unhide_session", lambda sid: None)
    monkeypatch.setattr(hook, "_controlling_tty", lambda: "")
    monkeypatch.setattr(hook, "_pid_tty", lambda pid: "/dev/ttys002")
    monkeypatch.setattr(hook, "_session_pid", lambda names: 222)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    return path


@pytest.mark.parametrize("event_name", ["PreToolUse", "PostToolUse", "PermissionRequest"])
def test_subagent_progress_preserves_foreground_wait(
    monkeypatch: pytest.MonkeyPatch, hook_registry: Path, event_name: str
) -> None:
    notifications: list[Any] = []
    monkeypatch.setattr(hook, "_ring_waiting_now", lambda *args: notifications.append(args))
    foreground = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(
        dict(foreground, hook_event_name="Notification", notification_type="permission_prompt", message="approve me"),
        "claude-code",
    )
    before = json.loads((hook_registry / "s1.json").read_text())
    hook._record_session_state(
        dict(foreground, hook_event_name=event_name, tool_name="Read", agent_id="background-a"),
        "claude-code",
    )
    after = json.loads((hook_registry / "s1.json").read_text())
    fields = ("status", "waiting_kind", "waiting_detail", "waiting_notified_at")
    assert {k: after.get(k) for k in fields} == {k: before.get(k) for k in fields}
    assert len(notifications) == 1

    hook._record_session_state(dict(foreground, hook_event_name="UserPromptSubmit"), "claude-code")
    assert json.loads((hook_registry / "s1.json").read_text())["status"] == "working"


@pytest.mark.parametrize(
    ("waiting_agent", "progress_agent", "expected"),
    [("a", "a", "working"), ("a", "b", "waiting")],
    ids=["own-wait-resolved", "other-agent-still-waiting"],
)
def test_subagent_progress_only_resolves_its_own_wait(
    hook_registry: Path, waiting_agent: str, progress_agent: str, expected: str
) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(
        dict(payload, hook_event_name="PermissionRequest", requires_action=True, agent_id=waiting_agent),
        "claude-code",
    )
    hook._record_session_state(
        dict(payload, hook_event_name="PostToolUse", tool_name="Read", agent_id=progress_agent),
        "claude-code",
    )
    assert json.loads((hook_registry / "s1.json").read_text())["status"] == expected


@pytest.mark.parametrize(
    ("first_agent", "second_agent"),
    [
        pytest.param("a", "b", id="two-subagents"),
        pytest.param("", "b", id="foreground-first"),
        pytest.param("a", "", id="foreground-second"),
    ],
)
def test_overlapping_waits_resolve_independently(hook_registry: Path, first_agent: str, second_agent: str) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    observed = []
    for agent, event in [
        (first_agent, "PermissionRequest"),
        (second_agent, "PermissionRequest"),
        (first_agent, "PostToolUse"),
        (second_agent, "PostToolUse"),
    ]:
        data: dict[str, Any] = dict(payload, hook_event_name=event, agent_id=agent)
        if event == "PermissionRequest":
            data.update(requires_action=True, tool_name="Bash", tool_input={"command": agent or "foreground"})
        hook._record_session_state(data, "claude-code")
        observed.append(json.loads((hook_registry / "s1.json").read_text())["status"])
    assert observed == ["waiting", "waiting", "waiting", "working"]


@pytest.mark.parametrize("background_event", ["PermissionRequest", "PostToolUse"])
def test_background_does_not_supply_or_clear_foreground_permission_detail(
    hook_registry: Path, background_event: str
) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(
        dict(payload, hook_event_name="PermissionRequest", tool_name="Bash", tool_input={"command": "git push"}),
        "claude-code",
    )
    hook._record_session_state(
        dict(
            payload,
            hook_event_name=background_event,
            agent_id="b",
            tool_name="Read",
            tool_input={"file_path": "README.md"},
        ),
        "claude-code",
    )
    hook._record_session_state(
        dict(
            payload,
            hook_event_name="Notification",
            notification_type="permission_prompt",
            message="Claude needs your permission",
        ),
        "claude-code",
    )
    row = json.loads((hook_registry / "s1.json").read_text())
    # An unqualified notification with competing candidates must not guess a command.
    expected = "Claude needs your permission" if background_event == "PermissionRequest" else "Bash: git push"
    assert row["waiting_detail"] == expected


@pytest.mark.parametrize("first_agent", ["", "a"])
def test_legacy_wait_survives_migration_and_other_agent_resolution(hook_registry: Path, first_agent: str) -> None:
    hook_registry.mkdir(parents=True)
    path = hook_registry / "s1.json"
    path.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "status": "waiting",
                "waiting_agent_id": first_agent,
                "waiting_kind": "question",
                "waiting_detail": "first question",
            }
        )
    )
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(
        dict(
            payload, hook_event_name="PermissionRequest", requires_action=True, agent_id="b", message="second question"
        ),
        "claude-code",
    )
    hook._record_session_state(dict(payload, hook_event_name="PostToolUse", agent_id=first_agent), "claude-code")
    row = json.loads(path.read_text())
    assert (row["status"], row["waiting_agent_id"], row["waiting_detail"]) == ("waiting", "b", "second question")


@pytest.mark.parametrize("owner", ["", "a", "b"])
def test_idle_notification_does_not_clear_overlapping_waits(hook_registry: Path, owner: str) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    for agent in ("a", "b"):
        hook._record_session_state(
            dict(payload, hook_event_name="PermissionRequest", requires_action=True, agent_id=agent), "claude-code"
        )
    path = hook_registry / "s1.json"
    before = json.loads(path.read_text())["waiting_requests"]
    hook._record_session_state(
        dict(payload, hook_event_name="Notification", notification_type="idle_prompt", agent_id=owner), "claude-code"
    )
    assert json.loads(path.read_text())["waiting_requests"] == before


def test_subagent_end_resolves_only_its_own_wait(hook_registry: Path) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    for agent in ("a", "b"):
        hook._record_session_state(
            dict(payload, hook_event_name="PermissionRequest", requires_action=True, agent_id=agent), "claude-code"
        )
    hook._record_session_state(dict(payload, hook_event_name="SessionEnd", agent_id="a"), "claude-code")
    row = json.loads((hook_registry / "s1.json").read_text())
    assert (row["status"], row["waiting_agent_id"]) == ("waiting", "b")


def test_other_wait_remains_visible_in_json_after_owner_resumes(
    monkeypatch: pytest.MonkeyPatch, hook_registry: Path
) -> None:
    import ring.cli as cli

    monkeypatch.setattr(registry, "RING_REGISTRY", hook_registry)
    monkeypatch.setattr(registry, "background_agent_session_ids", set)
    monkeypatch.setattr(cli, "running_agent_pids", list)
    monkeypatch.setattr(cli, "load_labels", dict)
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(dict(payload, hook_event_name="SessionStart"), "claude-code")
    for agent in ("a", "b"):
        hook._record_session_state(
            dict(
                payload,
                hook_event_name="PermissionRequest",
                requires_action=True,
                agent_id=agent,
                message=f"question {agent}",
            ),
            "claude-code",
        )
    hook._record_session_state(dict(payload, hook_event_name="PostToolUse", agent_id="a"), "claude-code")
    sessions = registry._hook_sessions(
        procs_by_provider={"claude-code": [("/work/app", "/dev/ttys002")]}, pids_by_provider={"claude-code": [222]}
    )
    assert [r.owner for r in sessions[0].waiting_requests] == ["agent:b"]
    snapshot = json.loads(cli.render_json(sessions))
    assert snapshot["counts"]["waiting"] == 1
    assert [(s["session_id"], s["status"], s["waiting_detail"]) for s in snapshot["sessions"]] == [
        ("s1", "waiting", "question b")
    ]


@pytest.mark.parametrize("event_name", ["SessionStart", "PreToolUse"])
def test_new_foreground_binding_discards_old_waits_and_pending_details(
    monkeypatch: pytest.MonkeyPatch, hook_registry: Path, event_name: str
) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(dict(payload, hook_event_name="SessionStart"), "claude-code")
    hook._record_session_state(
        dict(
            payload,
            hook_event_name="PermissionRequest",
            agent_id="b",
            tool_name="Bash",
            tool_input={"command": "old command"},
        ),
        "claude-code",
    )
    hook._record_session_state(
        dict(payload, hook_event_name="Notification", notification_type="permission_prompt", agent_id="b"),
        "claude-code",
    )
    if event_name == "PreToolUse":
        monkeypatch.setattr(hook, "_session_pid", lambda names: 333)
    hook._record_session_state(dict(payload, hook_event_name=event_name), "claude-code")
    row = json.loads((hook_registry / "s1.json").read_text())
    assert (row["status"], row["waiting_requests"], row["pending_permissions"]) == ("working", {}, [])


@pytest.mark.parametrize("agent_key", ["agent_type", "agentType"])
def test_unidentified_subagent_progress_cannot_resolve_known_wait(hook_registry: Path, agent_key: str) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(
        dict(payload, hook_event_name="PermissionRequest", requires_action=True, agent_id="a"), "claude-code"
    )
    hook._record_session_state(
        dict(payload, hook_event_name="PostToolUse", **{agent_key: "general-purpose"}), "claude-code"
    )
    row = json.loads((hook_registry / "s1.json").read_text())
    assert (row["status"], row["waiting_agent_id"]) == ("waiting", "a")


@pytest.mark.parametrize("notification_agent", ["", "a"])
def test_unique_pending_request_can_supply_wait_owner(hook_registry: Path, notification_agent: str) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(
        dict(
            payload,
            hook_event_name="PermissionRequest",
            agent_id="a",
            tool_name="Bash",
            tool_input={"command": "git push"},
        ),
        "claude-code",
    )
    hook._record_session_state(
        dict(
            payload, hook_event_name="Notification", notification_type="permission_prompt", agent_id=notification_agent
        ),
        "claude-code",
    )
    path = hook_registry / "s1.json"
    row = json.loads(path.read_text())
    assert (row["waiting_agent_id"], row["waiting_detail"]) == ("a", "Bash: git push")
    hook._record_session_state(dict(payload, hook_event_name="PostToolUse", agent_id="a"), "claude-code")
    assert json.loads(path.read_text())["status"] == "working"


@pytest.mark.parametrize("request_key", ["tool_use_id", "toolUseId"])
def test_pending_permission_clear_matches_tool_use_id(hook_registry: Path, request_key: str) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app", "agent_id": "a"}
    for request_id in ("one", "two"):
        hook._record_session_state(
            dict(
                payload,
                hook_event_name="PermissionRequest",
                tool_name="Bash",
                tool_input={"command": request_id},
                **{request_key: request_id},
            ),
            "claude-code",
        )
    hook._record_session_state(dict(payload, hook_event_name="PostToolUse", **{request_key: "one"}), "claude-code")
    hook._record_session_state(
        dict(payload, hook_event_name="Notification", notification_type="permission_prompt", **{request_key: "two"}),
        "claude-code",
    )
    row = json.loads((hook_registry / "s1.json").read_text())
    assert row["waiting_detail"] == "Bash: two"
    assert [item["request_id"] for item in row["pending_permissions"]] == ["two"]


@pytest.mark.parametrize("completion_id", ["", "not-a-pending-id"])
def test_ambiguous_tool_completion_keeps_pending_candidates(hook_registry: Path, completion_id: str) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app", "agent_id": "a"}
    for request_id in ("one", "two"):
        hook._record_session_state(
            dict(
                payload,
                hook_event_name="PermissionRequest",
                tool_use_id=request_id,
                tool_name="Bash",
                tool_input={"command": request_id},
            ),
            "claude-code",
        )
    hook._record_session_state(dict(payload, hook_event_name="PostToolUse", tool_use_id=completion_id), "claude-code")
    hook._record_session_state(
        dict(
            payload,
            hook_event_name="Notification",
            notification_type="permission_prompt",
            message="Claude needs your permission",
        ),
        "claude-code",
    )
    row = json.loads((hook_registry / "s1.json").read_text())
    assert row["waiting_detail"] == "Claude needs your permission"
    assert [item["request_id"] for item in row["pending_permissions"]] == ["one", "two"]


@pytest.mark.parametrize(
    ("age", "expected"),
    [
        pytest.param(120.0, "Bash: git push", id="at-ttl"),
        pytest.param(120.1, "Claude needs your permission", id="over-ttl"),
    ],
)
def test_legacy_pending_permission_respects_ttl(
    monkeypatch: pytest.MonkeyPatch, hook_registry: Path, age: float, expected: str
) -> None:
    hook_registry.mkdir(parents=True)
    path = hook_registry / "s1.json"
    path.write_text(
        json.dumps(
            {
                "session_id": "s1",
                "status": "working",
                "pending_permission_detail": "Bash: git push",
                "pending_permission_detail_at": 1000.0 - age,
            }
        )
    )
    monkeypatch.setattr("ring.hook.time.time", lambda: 1000.0)
    hook._record_session_state(
        {
            "session_id": "s1",
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission",
        },
        "claude-code",
    )
    assert json.loads(path.read_text())["waiting_detail"] == expected


def test_new_session_does_not_inherit_old_notification_cooldown(
    monkeypatch: pytest.MonkeyPatch, hook_registry: Path
) -> None:
    notices: list[str] = []
    monkeypatch.setattr(hook, "get_config", lambda: Config(waiting_cooldown_seconds=60))
    monkeypatch.setattr("ring.hook.time.time", lambda: 1000.0)
    monkeypatch.setattr(hook, "_ring_waiting_now", lambda event, payload, action: notices.append(event.session_id))
    payload = {"session_id": "s1", "cwd": "/work/app"}
    waiting = dict(payload, hook_event_name="PermissionRequest", requires_action=True)
    hook._record_session_state(waiting, "claude-code")
    hook._record_session_state(dict(payload, hook_event_name="SessionStart"), "claude-code")
    hook._record_session_state(waiting, "claude-code")
    assert notices == ["s1", "s1"]


def test_new_wait_notification_describes_its_owner_not_the_displayed_wait(
    monkeypatch: pytest.MonkeyPatch, hook_registry: Path
) -> None:
    monkeypatch.setattr(hook, "get_config", lambda: Config(waiting_cooldown_seconds=0))
    notices: list[str] = []
    monkeypatch.setattr(
        hook, "_ring_waiting_now", lambda event, payload, action: notices.append(payload["waiting_detail"])
    )
    payload = {"session_id": "s1", "cwd": "/work/app"}
    for agent in ("a", "b"):
        hook._record_session_state(
            dict(
                payload,
                hook_event_name="PermissionRequest",
                requires_action=True,
                agent_id=agent,
                message=f"question {agent}",
            ),
            "claude-code",
        )
    row = json.loads((hook_registry / "s1.json").read_text())
    assert row["waiting_detail"] == "question a"
    assert notices == ["question a", "question b"]


@pytest.mark.parametrize("agent_key", ["agent_id", "agentId", "agent_type", "agentType"])
def test_subagent_preserves_foreground_terminal_and_transcript(
    monkeypatch: pytest.MonkeyPatch, hook_registry: Path, agent_key: str
) -> None:
    monkeypatch.setenv("TMUX_PANE", "%1")
    hook._record_session_state(
        {"session_id": "s1", "cwd": "/work/app", "hook_event_name": "PreToolUse", "transcript_path": "/host.jsonl"},
        "claude-code",
    )
    before = json.loads((hook_registry / "s1.json").read_text())
    monkeypatch.setenv("TMUX_PANE", "%2")
    hook._record_session_state(
        {
            "session_id": "s1",
            "cwd": "/background",
            "hook_event_name": "PostToolUse",
            "transcript_path": "/background.jsonl",
            "tty": "/dev/ttys999",
            agent_key: "background-a",
        },
        "claude-code",
    )
    after = json.loads((hook_registry / "s1.json").read_text())
    fields = ("agent_pid", "tty", "tmux_pane", "cwd", "origin_cwd", "source_path")
    assert {k: after.get(k) for k in fields} == {k: before.get(k) for k in fields}


@pytest.mark.parametrize(
    ("event_name", "new_pid", "expected_pid", "expected_tty"),
    [
        pytest.param("Notification", None, 222, "/dev/ttys002", id="unknown-lookup-keeps-binding"),
        pytest.param("SessionStart", None, None, None, id="new-session-does-not-inherit-unknown-binding"),
        pytest.param("PreToolUse", 333, 333, None, id="new-pid-does-not-inherit-old-terminal"),
    ],
)
def test_foreground_binding_survives_lookup_failure_but_not_rebinding(
    monkeypatch: pytest.MonkeyPatch,
    hook_registry: Path,
    event_name: str,
    new_pid: int | None,
    expected_pid: int | None,
    expected_tty: str | None,
) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(
        dict(payload, hook_event_name="Notification", notification_type="permission_prompt"),
        "claude-code",
    )
    monkeypatch.setattr(hook, "_session_pid", lambda names: new_pid)
    monkeypatch.setattr(hook, "_pid_tty", lambda pid: "")
    hook._record_session_state(
        dict(payload, hook_event_name=event_name, notification_type="idle_prompt"),
        "claude-code",
    )
    after = json.loads((hook_registry / "s1.json").read_text())
    assert (after.get("agent_pid"), after.get("tty")) == (expected_pid, expected_tty)


def _concurrent_hook_writer(
    registry_path: Path,
    first_reading: ProcessEvent,
    second_started: ProcessEvent,
    second_reading: ProcessEvent,
    release_first: ProcessEvent,
    observations: Any,
    notification_type: str,
) -> None:
    """spawn 子程序也獨立隔離 registry、通知與設定，不繼承 pytest 的全域 mock。"""
    previous_row = hook._previous_row

    def read_previous(path: Path) -> dict[str, Any]:
        row = previous_row(path)
        role = multiprocessing.current_process().name
        observations.put((role, row.get("status")))
        if role == "first":
            first_reading.set()
            assert release_first.wait(5)
        else:
            second_reading.set()
        return row

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(hook, "RING_REGISTRY", registry_path)
        patch.setattr(hook, "get_config", lambda: Config())
        patch.setattr(hook, "unhide_session", lambda sid: None)
        patch.setattr(hook, "log_transition", lambda *args: None)
        patch.setattr(hook, "_session_pid", lambda names: None)
        patch.setattr(hook, "_controlling_tty", lambda: "")
        patch.setattr(hook, "_previous_row", read_previous)
        patch.setattr(hook, "_ring_waiting_now", lambda *args: None)
        if multiprocessing.current_process().name == "second":
            second_started.set()
        hook._record_session_state(
            {
                "session_id": "s1",
                "cwd": "/work/app",
                "hook_event_name": "Notification",
                "notification_type": notification_type,
            },
            "claude-code",
        )


def test_hook_concurrent_processes_read_committed_predecessor(hook_registry: Path) -> None:
    """第二個 writer 必須在第一個提交後才讀 prev_row，不能只保護 rename。"""
    ctx = multiprocessing.get_context("spawn")
    first_reading, second_started, second_reading, release_first = (ctx.Event() for _ in range(4))
    observations = ctx.Queue()
    args = (hook_registry, first_reading, second_started, second_reading, release_first, observations)
    workers = [
        ctx.Process(name="first", target=_concurrent_hook_writer, args=(*args, "permission_prompt")),
        ctx.Process(name="second", target=_concurrent_hook_writer, args=(*args, "idle_prompt")),
    ]
    try:
        workers[0].start()
        assert first_reading.wait(5)
        workers[1].start()
        assert second_started.wait(5)
        # 讓第二個 writer 嘗試讀取；有鎖時必須等待，不可讀到未提交的空 row。
        entered_early = second_reading.wait(0.2)
    finally:
        release_first.set()
        for worker in workers:
            if worker.pid is not None:
                worker.join(5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(5)
    assert [worker.exitcode for worker in workers] == [0, 0]
    assert not entered_early
    read_states = dict(observations.get(timeout=2) for _ in workers)
    observations.close()
    observations.join_thread()
    assert read_states == {"first": None, "second": "waiting"}
    assert json.loads((hook_registry / "s1.json").read_text())["status"] == "waiting"


@pytest.mark.parametrize("failure_stage", ["write", "replace", "end"])
def test_hook_io_failure_does_not_prevent_delegation(
    monkeypatch: pytest.MonkeyPatch,
    hook_registry: Path,
    failure_stage: str,
) -> None:
    def fail(*args: Any, **kwargs: Any) -> None:
        raise OSError("registry unavailable")

    calls: list[str] = []

    def delegate(raw: str, provider: str) -> int:
        calls.append(provider)
        return 0

    monkeypatch.setattr(hook, "_delegate_to_agent_hooks", delegate)
    method = {"write": "write_text", "replace": "replace", "end": "unlink"}[failure_stage]
    monkeypatch.setattr(Path, method, fail)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "SessionEnd" if failure_stage == "end" else "Stop"})
    assert hook.run_hook() == 0
    assert calls == ["claude-code"]


def test_hook_notification_runs_after_unlock(monkeypatch: pytest.MonkeyPatch, hook_registry: Path) -> None:
    notified: list[str] = []

    def notify(event: Any, payload: dict[str, Any], last_action: str) -> None:
        # 即使 notifier 很慢，前景的下一筆 hook 也必須能取得 lock。
        with (hook_registry / "s1.json.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(handle, fcntl.LOCK_UN)
        notified.append(event.session_id)
        hook._record_session_state(
            {"session_id": "s1", "cwd": "/work/app", "hook_event_name": "UserPromptSubmit"},
            "claude-code",
        )

    monkeypatch.setattr(hook, "_ring_waiting_now", notify)
    hook._record_session_state(
        {
            "session_id": "s1",
            "cwd": "/work/app",
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
        },
        "claude-code",
    )
    assert notified == ["s1"]
    assert json.loads((hook_registry / "s1.json").read_text())["status"] == "working"


@pytest.mark.parametrize("initial_status", ["working", "waiting"])
def test_subagent_session_end_cannot_delete_host(hook_registry: Path, initial_status: str) -> None:
    payload: dict[str, Any] = {"session_id": "s1", "hook_event_name": "PreToolUse"}
    if initial_status == "waiting":
        payload["requires_action"] = True
    hook._record_session_state(payload, "claude-code")
    path = hook_registry / "s1.json"
    before = path.read_bytes()
    assert json.loads(before)["status"] == initial_status
    hook._record_session_state({"session_id": "s1", "hook_event_name": "SessionEnd", "agent_id": "a"}, "claude-code")
    assert path.read_bytes() == before

    hook._record_session_state({"session_id": "s1", "hook_event_name": "SessionEnd"}, "claude-code")
    assert not path.exists()


def test_lookup_failure_does_not_revive_dead_owner(monkeypatch: pytest.MonkeyPatch, hook_registry: Path) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app", "hook_event_name": "Notification"}
    hook._record_session_state(dict(payload, notification_type="permission_prompt"), "claude-code")
    monkeypatch.setattr(hook, "_session_pid", lambda names: None)
    hook._record_session_state(dict(payload, notification_type="idle_prompt"), "claude-code")
    monkeypatch.setattr(registry, "RING_REGISTRY", hook_registry)
    monkeypatch.setattr(registry, "background_agent_session_ids", set)

    sessions = registry._hook_sessions(
        procs_by_provider={"claude-code": [("/work/app", "/dev/ttys003")]},
        pids_by_provider={"claude-code": [333]},
    )
    assert [(s.session_id, s.agent_pid, s.status) for s in sessions] == [("s1", 222, Status.ENDED)]


def test_subagent_without_tty_keeps_host_focusable(monkeypatch: pytest.MonkeyPatch, hook_registry: Path) -> None:
    payload = {"session_id": "s1", "cwd": "/work/app"}
    hook._record_session_state(dict(payload, hook_event_name="PreToolUse"), "claude-code")
    hook._record_session_state(dict(payload, hook_event_name="PostToolUse", agent_id="a"), "claude-code")
    monkeypatch.setattr(registry, "RING_REGISTRY", hook_registry)
    monkeypatch.setattr(registry, "background_agent_session_ids", set)
    current = registry._hook_sessions(
        procs_by_provider={"claude-code": [("/work/app", "/dev/ttys002")]},
        pids_by_provider={"claude-code": [222]},
    )[0]
    scan = Session("s1", "/work/app", Status.WORKING, current.last_active + 1, "tool", "scan", _tail_kind="interrupted")
    session = sources._merge_duplicate_session(current, scan)
    matched_ttys: list[str] = []

    def resolve(tty: str) -> tuple[str, int]:
        matched_ttys.append(tty)
        return "/tmp/fake-kitty", 1

    monkeypatch.setattr(kitty, "resolve_window", resolve)
    monkeypatch.setattr(kitty, "_run", lambda command: subprocess.CompletedProcess(command, 0, "", ""))
    monkeypatch.setattr(kitty, "osascript", lambda script: (0, "", ""))
    assert kitty.focuser.try_focus(session) == (True, "kitty 1")
    assert matched_ttys == ["/dev/ttys002"]


def test_hook_event_writes_heartbeat_and_source_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path / "registry")
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "transcript_path": str(transcript),
        },
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "registry" / "s1.json").read_text())
    assert data["heartbeat_at"] == data["last_active"]
    assert data["source_path"] == str(transcript)


def test_hook_event_unhides_manually_deleted_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    unhidden: list[str] = []
    monkeypatch.setattr(hook, "unhide_session", lambda sid: unhidden.append(sid))
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"})

    assert hook.run_hook() == 0

    assert unhidden == ["s1"]


def test_stop_with_requires_action_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x", "requires_action": True})

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value


def test_waiting_for_next_step_writes_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Notification",
            "cwd": "/x",
            "waiting_for": "next_step",
        },
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value
    assert data["waiting_for"] == "next_step"


def test_waiting_for_permission_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Notification",
            "cwd": "/x",
            "waiting_for": "permission",
        },
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_for"] == "permission"


def test_permission_notification_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "permission_prompt", "cwd": "/x"},
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value


def test_agent_needs_input_notification_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """背景 agent 停下來要輸入的 Notification → 🔴 等你（question 類），不是 🟡 閒置。

    型別實證：claude 2.1.215 binary 含 ``agent_needs_input`` 字串。
    """
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "agent_needs_input", "cwd": "/x"},
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "question"


def test_regular_notification_writes_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "auth_success", "cwd": "/x"},
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value


def _bare_permission_request(session_id: str = "s1", *, subagent: bool = False) -> dict[str, Any]:
    """真實 Claude Code 裸 PermissionRequest 的欄位形狀（取自 hook_payloads.jsonl 實錄）。

    權限「判定」時就發、多數瞬間被 policy 自動放行——subagent 觸發的帶 agent_id/agent_type，
    主執行緒的不帶。兩種都沒有 requires_action / waiting_for 這類顯式訊號。
    """
    payload: dict[str, Any] = {
        "session_id": session_id,
        "transcript_path": f"/nonexistent/{session_id}.jsonl",
        "cwd": "/x",
        "prompt_id": "f6faddb7-473f-4345-9518-4e4c3ea58554",
        "permission_mode": "acceptEdits",
        "effort": {"level": "high"},
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "grep -n foo src/*.py", "description": "Search for foo"},
        "permission_suggestions": [],
    }
    if subagent:
        payload["agent_id"] = "a490599d8a4bbc07c"
        payload["agent_type"] = "general-purpose"
    return payload


def test_claude_bare_permission_request_writes_working(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """主執行緒的裸 PermissionRequest（無 agent_id）→ 🟢 WORKING：權限判定不等於停下來等人。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, _bare_permission_request())

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WORKING.value
    assert "waiting_detail" not in data


def test_claude_subagent_bare_permission_request_writes_working(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """subagent 觸發的裸 PermissionRequest（帶 agent_id/agent_type）→ 🟢 WORKING。

    這是 48 小時內單一 session 翻轉 105 次的假「等你」來源：subagent 跑唯讀工具，
    每次呼叫都發 PermissionRequest、幾秒內自動放行，不該閃紅。
    """
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, _bare_permission_request(subagent=True))

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WORKING.value
    assert "waiting_detail" not in data


def test_ask_user_question_permission_request_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AskUserQuestion 包在 PermissionRequest 裡 → 🔴 WAITING，detail 帶問題內容。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "cwd": "/x",
            "permission_mode": "acceptEdits",
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "要用哪個 auth 方案？", "options": [{"label": "A"}]}]},
        },
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert "要用哪個 auth 方案" in data["waiting_detail"]


def _permission_prompt_notification(session_id: str = "s1", cwd: str = "/proj") -> dict[str, Any]:
    """真實 Claude Code permission_prompt Notification 的欄位形狀（取自 hook_payloads.jsonl 實錄）。

    真的停下來等人時，裸 PermissionRequest 後 ~6 秒必有這個事件——它才是「等你」的訊號。
    """
    return {
        "session_id": session_id,
        "cwd": cwd,
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "message": "Claude needs your permission",
    }


class _SpyNotifier:
    """記下被 send 的 session，供「hook 有沒有就地發通知」斷言。"""

    name = "spy"

    def __init__(self) -> None:
        self.sent: list[Any] = []

    def available(self) -> bool:
        return True

    def supports_click(self) -> bool:
        return True

    def send(self, sessions: list[Any]) -> None:
        self.sent.extend(sessions)


def test_waiting_event_delivers_notification_in_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """轉 🔴 等你時，hook 就地發系統通知（不必等看板輪詢）。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    spy = _SpyNotifier()
    monkeypatch.setattr("ring.notify._NOTIFIERS", [spy])
    _feed(monkeypatch, _permission_prompt_notification())

    assert hook.run_hook() == 0

    assert len(spy.sent) == 1
    assert spy.sent[0].session_id == "s1"
    assert spy.sent[0].status is Status.WAITING
    assert spy.sent[0].cwd == "/proj"


def test_non_waiting_event_does_not_notify_in_hook(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """非等你狀態（Stop → 🟡 閒置）不該觸發系統通知。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    spy = _SpyNotifier()
    monkeypatch.setattr("ring.notify._NOTIFIERS", [spy])
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/proj"})

    assert hook.run_hook() == 0

    assert spy.sent == []


def test_run_hook_flushes_due_queue_headless(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """headless（沒開 TUI）：queue 有累積時，任何 hook 事件開頭都要懶惰 flush 一次彙總。"""
    import ring.notify_queue as notify_queue

    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    notify_queue.enqueue([Session("waiting-1", "/x", Status.WAITING, 0.0, "→ Edit", "hook")])
    summary_calls: list[int] = []
    monkeypatch.setattr("ring.notify.notify_summary", lambda count, sample: summary_calls.append(count))
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/proj"})

    assert hook.run_hook() == 0

    assert summary_calls == [1]
    assert notify_queue.peek_count() == 0


def test_run_hook_does_not_flush_while_quiet_active(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """quiet active 時跑 hook 主流程 → 不 flush（queue 保留）。"""
    import ring.notify_queue as notify_queue

    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    notify_queue.enqueue([Session("waiting-1", "/x", Status.WAITING, 0.0, "→ Edit", "hook")])
    notify_queue.set_quiet(None)
    summary_calls: list[int] = []
    monkeypatch.setattr("ring.notify.notify_summary", lambda count, sample: summary_calls.append(count))
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/proj"})

    assert hook.run_hook() == 0

    assert summary_calls == []
    assert notify_queue.peek_count() == 1


def test_waiting_flap_within_cooldown_suppresses_second_hook_notification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """反例：session 在 working↔waiting 間快速翻轉——冷卻期內第二次轉 waiting 不該再發系統通知。

    背景 subagent 的權限請求會讓 session 以 30 秒～2 分鐘頻率翻轉；沒有冷卻期時，
    每次轉入 waiting 都由 hook 發一則系統通知，轟炸使用者。
    """
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    spy = _SpyNotifier()
    monkeypatch.setattr("ring.notify._NOTIFIERS", [spy])

    _feed(monkeypatch, _permission_prompt_notification())
    assert hook.run_hook() == 0
    assert len(spy.sent) == 1

    # 翻轉：權限請求解決 → 離開 waiting（Stop → idle）。時間戳要跟著帶進新 row。
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/proj"})
    assert hook.run_hook() == 0
    row = json.loads((tmp_path / "s1.json").read_text())
    assert "waiting_notified_at" in row, "離開 waiting 時要保留上次通知時間戳，否則冷卻判斷失憶"

    # 幾秒內又翻回 waiting（遠小於預設 180 秒冷卻期）→ 不再發
    _feed(monkeypatch, _permission_prompt_notification())
    assert hook.run_hook() == 0

    assert len(spy.sent) == 1, "冷卻期內再轉 waiting 不該再發一則系統通知"


def test_waiting_realerts_after_cooldown_expired(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """距上次通知已滿冷卻期 → 再轉 waiting 照常發通知。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    spy = _SpyNotifier()
    monkeypatch.setattr("ring.notify._NOTIFIERS", [spy])

    _feed(monkeypatch, _permission_prompt_notification())
    assert hook.run_hook() == 0
    assert len(spy.sent) == 1

    # 把 row 的上次通知時間改成遠早於冷卻期（模擬時間流逝，不 patch 時鐘）
    row_path = tmp_path / "s1.json"
    row = json.loads(row_path.read_text())
    row["waiting_notified_at"] = row["waiting_notified_at"] - 999.0
    row_path.write_text(json.dumps(row))

    _feed(monkeypatch, _permission_prompt_notification())
    assert hook.run_hook() == 0

    assert len(spy.sent) == 2, "冷卻期滿後再轉 waiting 要照常通知"
    # 放行同時要把時間戳推進，下一輪冷卻從這次通知起算
    new_ts = json.loads(row_path.read_text())["waiting_notified_at"]
    assert new_ts > row["waiting_notified_at"]


def test_waiting_cooldown_zero_notifies_every_transition(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """waiting_cooldown_seconds=0 = 關閉冷卻：每次轉 waiting 都發（現行為）。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setattr("ring.hook.get_config", lambda: Config(waiting_cooldown_seconds=0))
    spy = _SpyNotifier()
    monkeypatch.setattr("ring.notify._NOTIFIERS", [spy])

    _feed(monkeypatch, _permission_prompt_notification())
    assert hook.run_hook() == 0
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/proj"})
    assert hook.run_hook() == 0
    _feed(monkeypatch, _permission_prompt_notification())
    assert hook.run_hook() == 0

    assert len(spy.sent) == 2


def test_waiting_notifies_when_legacy_row_lacks_timestamp_field(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """向後相容：舊版 row 沒有 waiting_notified_at 欄位 = 從未通知過 → 照發，並補上欄位。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    spy = _SpyNotifier()
    monkeypatch.setattr("ring.notify._NOTIFIERS", [spy])
    (tmp_path / "s1.json").write_text(
        json.dumps({"session_id": "s1", "provider": "claude-code", "cwd": "/proj", "status": "working"})
    )

    _feed(monkeypatch, _permission_prompt_notification())
    assert hook.run_hook() == 0

    assert len(spy.sent) == 1
    row = json.loads((tmp_path / "s1.json").read_text())
    assert isinstance(row.get("waiting_notified_at"), float)


def test_ask_user_question_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"id": "choice", "options": [{"label": "A"}]}]},
            "cwd": "/x",
        },
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value


def test_user_prompt_writes_working(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert json.loads((tmp_path / "s1.json").read_text())["status"] == Status.WORKING.value


def test_codex_provider_writes_qualified_session(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "thread-1", "event": "Stop", "cwd": "/repo"})

    assert hook.run_hook(provider="codex") == 0

    data = json.loads((tmp_path / "codex:thread-1.json").read_text())
    assert data["session_id"] == "codex:thread-1"
    assert data["provider"] == "codex"
    assert data["status"] == Status.IDLE.value


def test_codex_bare_permission_request_stays_working(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Codex 權限 policy 自動放行時也會送 hook；裸事件不代表真的停下來等人（規則不分 provider）。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "thread-1", "event": "PermissionRequest", "cwd": "/repo"})

    assert hook.run_hook(provider="codex") == 0

    data = json.loads((tmp_path / "codex:thread-1.json").read_text())
    assert data["status"] == Status.WORKING.value


def test_codex_explicit_permission_request_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "thread-1",
            "event": "PermissionRequest",
            "cwd": "/repo",
            "requires_action": True,
        },
    )

    assert hook.run_hook(provider="codex") == 0

    data = json.loads((tmp_path / "codex:thread-1.json").read_text())
    assert data["status"] == Status.WAITING.value


def test_payload_provider_overrides_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"provider": "codex", "session_id": "thread-2", "event": "UserPromptSubmit", "cwd": "/repo"})

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "codex:thread-2.json").read_text())
    assert data["status"] == Status.WORKING.value


def test_session_start_source_not_treated_as_provider(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Claude SessionStart 帶 source='startup' → 不可被誤當 provider，session_id 不該有 startup: 前綴。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s9", "hook_event_name": "SessionStart", "source": "startup", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert not (tmp_path / "startup:s9.json").exists(), "不該生出 startup: 幽靈檔"
    data = json.loads((tmp_path / "s9.json").read_text())
    assert data["session_id"] == "s9"
    assert data["provider"] == "claude-code"
    assert data["status"] == Status.WORKING.value


def test_session_end_deletes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    (tmp_path / "s2.json").write_text("{}")
    _feed(monkeypatch, {"session_id": "s2", "hook_event_name": "SessionEnd", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert not (tmp_path / "s2.json").exists()


def test_unknown_event_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s3", "hook_event_name": "SomethingWeird", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert not (tmp_path / "s3.json").exists()


def test_pre_tool_use_non_action_writes_working(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """非 action 的 PreToolUse（一般工具）→ 🟢 WORKING，清掉上一個卡住的 WAITING。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PreToolUse", "tool_name": "Bash", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert json.loads((tmp_path / "s1.json").read_text())["status"] == Status.WORKING.value


def test_post_tool_use_writes_working(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PostToolUse（工具跑完、使用者已放行）→ 🟢 WORKING，止住重複通知。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert json.loads((tmp_path / "s1.json").read_text())["status"] == Status.WORKING.value


def test_delegates_to_agent_hooks_when_backend_set(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """notify_backend="agent-hooks" + binary 在 → 透傳 payload 給 agent-hooks，且狀態照樣寫。"""
    import types

    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setattr("ring.hook.get_config", lambda: Config(notify_backend="agent-hooks"))
    monkeypatch.setattr("ring.hook.shutil.which", lambda name: "/bin/agent-hooks" if name == "agent-hooks" else None)
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(cmd: list[str], **kw: object) -> object:
        calls.append((cmd, kw.get("input")))  # type: ignore[arg-type]
        return types.SimpleNamespace(returncode=0)

    monkeypatch.setattr("ring.hook.subprocess.run", fake_run)
    # payload 帶 tty；_session_pid 已由 autouse fixture 隔離，不會把 ps 混進 calls。
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/x", "tty": "/dev/ttys1"})

    assert hook.run_hook() == 0
    assert len(calls) == 1
    assert calls[0][0][:2] == ["agent-hooks", "callback"]
    assert "--provider" in calls[0][0]
    assert calls[0][1] is not None and "PermissionRequest" in calls[0][1]  # 原始 payload 被透傳
    # 狀態照寫（裸 PermissionRequest 現在是 🟢 WORKING；委派與狀態記錄互不影響）
    assert json.loads((tmp_path / "s1.json").read_text())["status"] == Status.WORKING.value


def test_no_delegation_when_backend_not_agent_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setattr("ring.hook.get_config", lambda: Config(notify_backend="auto"))
    calls: list[object] = []
    monkeypatch.setattr("ring.hook.subprocess.run", lambda *a, **k: calls.append((a, k)))
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/x", "tty": "/dev/ttys1"})

    assert hook.run_hook() == 0
    assert calls == []  # auto → 不委派
    assert (tmp_path / "s1.json").exists()  # 但狀態照樣寫


def test_no_delegation_when_agent_hooks_absent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setattr("ring.hook.get_config", lambda: Config(notify_backend="agent-hooks"))
    monkeypatch.setattr("ring.hook.shutil.which", lambda name: None)  # agent-hooks 沒裝
    calls: list[object] = []
    monkeypatch.setattr("ring.hook.subprocess.run", lambda *a, **k: calls.append((a, k)))
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/x", "tty": "/dev/ttys1"})

    assert hook.run_hook() == 0
    assert calls == []  # binary 不在 → 不委派
    assert (tmp_path / "s1.json").exists()  # 狀態照樣寫（看板仍可見），你回終端答


def test_malformed_stdin_never_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("not json at all"))
    assert hook.run_hook() == 0  # hook 永遠不擋住 session


# ---------------------------------------------------------------------------
# _is_ring_hook_command
# ---------------------------------------------------------------------------


def test_is_ring_hook_command_true_simple() -> None:
    assert _is_ring_hook_command("ring hook") is True


def test_is_ring_hook_command_true_full_path() -> None:
    assert _is_ring_hook_command("/usr/local/bin/ring hook") is True


def test_is_ring_hook_command_true_tilde_path() -> None:
    assert _is_ring_hook_command("~/.local/bin/ring hook") is True


def test_is_ring_hook_command_false_other_tool() -> None:
    assert _is_ring_hook_command("some-other-tool hook") is False


def test_is_ring_hook_command_false_ring_only() -> None:
    assert _is_ring_hook_command("ring") is False


def test_is_ring_hook_command_false_empty() -> None:
    assert _is_ring_hook_command("") is False


def test_is_ring_hook_command_false_ring_other_subcommand() -> None:
    assert _is_ring_hook_command("ring install-hooks") is False


# ---------------------------------------------------------------------------
# install_hooks
# ---------------------------------------------------------------------------


def test_install_hooks_fresh(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """全新環境裝一次 → 5 個 event 各有一條 ring hook。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = install_hooks()
    assert rc == 0
    data = json.loads(settings.read_text())
    for event in hook._HOOK_EVENTS:
        cmds = [h["command"] for g in data["hooks"][event] for h in g.get("hooks", [])]
        assert "ring hook" in cmds, f"event {event} 應有 ring hook"
    assert "PermissionRequest" in data["hooks"]
    assert "PreToolUse" in data["hooks"]
    assert "已註冊" in capsys.readouterr().out


def test_install_hooks_old_fullpath_replaced(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """已有舊 full-path 條目的環境再裝 → 舊條目被換成 ring hook，不重複、不殘留。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    _settings_with_ring_hook(settings, cmd="/usr/local/bin/ring hook")
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = install_hooks()
    assert rc == 0
    data = json.loads(settings.read_text())
    for event in hook._HOOK_EVENTS:
        all_cmds = [h["command"] for g in data["hooks"][event] for h in g.get("hooks", [])]
        assert "/usr/local/bin/ring hook" not in all_cmds, "舊 full-path 不應殘留"
        assert all_cmds.count("ring hook") == 1, f"event {event} 應只有一條 ring hook"


def test_install_hooks_already_installed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """已是 ring hook 的環境再裝 → 印「已經裝過」、無變更、不寫檔。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    _settings_with_ring_hook(settings)
    mtime_before = settings.stat().st_mtime
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = install_hooks()
    assert rc == 0
    assert settings.stat().st_mtime == mtime_before, "不應寫檔"
    assert "已經裝過" in capsys.readouterr().out


def test_install_hooks_upgrades_stale_timeout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """舊版裝的 timeout=10 條目 → 再裝會自我修復成現值（command 相同也要更新）。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    _settings_with_ring_hook(settings, timeout=10)
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = install_hooks()
    assert rc == 0
    data = json.loads(settings.read_text())
    for event in hook._HOOK_EVENTS:
        timeouts = [h.get("timeout") for g in data["hooks"][event] for h in g.get("hooks", [])]
        assert timeouts == [hook._HOOK_TIMEOUT], f"event {event} 的 timeout 應升到 {hook._HOOK_TIMEOUT}"
    assert "已註冊" in capsys.readouterr().out


def test_install_hooks_warns_on_coresident_handler(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """裝 ring hook 時，若 PermissionRequest/Notification 上還掛著別的工具 → 警告會重複觸發。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    data = {
        "hooks": {
            "PermissionRequest": [{"hooks": [{"type": "command", "command": "other-notifier callback"}]}],
        }
    }
    settings.write_text(json.dumps(data))
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    install_hooks()
    out = capsys.readouterr().out
    assert "other-notifier callback" in out
    assert "重複觸發" in out
    # 警告歸警告，他人 hook 仍保留不動
    result = json.loads(settings.read_text())
    pr_cmds = [h["command"] for g in result["hooks"]["PermissionRequest"] for h in g.get("hooks", [])]
    assert "other-notifier callback" in pr_cmds


def test_install_hooks_no_warning_when_clean(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """沒有共存的互動 hook → 不印警告。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    install_hooks()
    assert "重複觸發" not in capsys.readouterr().out


def test_install_hooks_preserves_other_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """別人的 hook 條目原封不動。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    other_hook = {"type": "command", "command": "other-tool run", "timeout": 5}
    data = {
        "hooks": {
            "Stop": [{"hooks": [other_hook]}],
        }
    }
    settings.write_text(json.dumps(data))
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    install_hooks()
    result = json.loads(settings.read_text())
    stop_cmds = [h["command"] for g in result["hooks"]["Stop"] for h in g.get("hooks", [])]
    assert "other-tool run" in stop_cmds, "他人 hook 應保留"


def test_install_hooks_dry_run_no_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """dry_run 不寫檔。"""
    settings = tmp_path / ".claude" / "settings.json"
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = install_hooks(dry_run=True)
    assert rc == 0
    assert not settings.exists(), "dry_run 不應建立/寫 settings.json"
    out = capsys.readouterr().out
    assert "dry-run" in out


def test_install_hooks_invalid_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """settings.json 非法 JSON → 回 1。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("not json {{")
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    assert install_hooks() == 1


# ---------------------------------------------------------------------------
# install_hooks — Codex target
# ---------------------------------------------------------------------------


def test_install_hooks_includes_codex_when_codex_dir_exists(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """~/.codex 存在 → 也把 `ring hook --provider codex` 裝進 ~/.codex/hooks.json。"""
    (tmp_path / ".codex").mkdir()
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    assert install_hooks() == 0
    codex = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    for event in hook._CODEX_HOOK_EVENTS:
        cmds = [h["command"] for g in codex["hooks"][event] for h in g.get("hooks", [])]
        assert "ring hook --provider codex" in cmds, f"codex event {event} 應有 ring hook --provider codex"
    # Claude 也照裝（同一次 install）
    claude = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    claude_cmds = [h["command"] for g in claude["hooks"]["PermissionRequest"] for h in g.get("hooks", [])]
    assert "ring hook" in claude_cmds


def test_install_hooks_skips_codex_when_no_codex_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """沒有 ~/.codex（沒在用 Codex）→ 不建立 ~/.codex/hooks.json。"""
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)
    install_hooks()
    assert not (tmp_path / ".codex" / "hooks.json").exists()


def test_install_hooks_codex_preserves_other_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Codex hooks.json 裡別人的條目（agent-hooks）保留，ring 條目合併進去。"""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    existing = {
        "hooks": {
            "PermissionRequest": [{"hooks": [{"type": "command", "command": "agent-hooks callback --provider codex"}]}]
        }
    }
    (codex_dir / "hooks.json").write_text(json.dumps(existing))
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    install_hooks()
    codex = json.loads((codex_dir / "hooks.json").read_text())
    cmds = [h["command"] for g in codex["hooks"]["PermissionRequest"] for h in g.get("hooks", [])]
    assert "agent-hooks callback --provider codex" in cmds  # 他人保留
    assert "ring hook --provider codex" in cmds  # ring 加入


def test_uninstall_hooks_removes_from_codex(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """uninstall 也清掉 Codex 的 ring hook 條目。"""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    entry = [{"hooks": [{"type": "command", "command": "ring hook --provider codex"}]}]
    data = {"hooks": dict.fromkeys(hook._CODEX_HOOK_EVENTS, entry)}
    (codex_dir / "hooks.json").write_text(json.dumps(data))
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    uninstall_hooks()
    codex = json.loads((codex_dir / "hooks.json").read_text())
    for event in hook._CODEX_HOOK_EVENTS:
        cmds = [h.get("command") for g in codex["hooks"].get(event, []) for h in g.get("hooks", [])]
        assert "ring hook --provider codex" not in cmds


# ---------------------------------------------------------------------------
# uninstall_hooks
# ---------------------------------------------------------------------------


def test_uninstall_hooks_removes_new_form(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """有 ring hook（新 ring hook）→ 全部清掉，寫檔。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    _settings_with_ring_hook(settings)
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = uninstall_hooks()
    assert rc == 0
    data = json.loads(settings.read_text())
    for event in hook._HOOK_EVENTS:
        all_cmds = [h.get("command") for g in data["hooks"].get(event, []) for h in g.get("hooks", [])]
        assert "ring hook" not in all_cmds, f"event {event} 的 ring hook 應被移除"
    assert "已移除" in capsys.readouterr().out


def test_uninstall_hooks_removes_old_fullpath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """有舊 full-path ring hook → 也全部清掉。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    _settings_with_ring_hook(settings, cmd="/usr/local/bin/ring hook")
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = uninstall_hooks()
    assert rc == 0
    data = json.loads(settings.read_text())
    for event in hook._HOOK_EVENTS:
        all_cmds = [h.get("command") for g in data["hooks"].get(event, []) for h in g.get("hooks", [])]
        assert "/usr/local/bin/ring hook" not in all_cmds


def test_uninstall_hooks_no_ring_entries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """沒有 ring 條目 → 無變更、回 0。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    data = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "other-tool run"}]}]}}
    settings.write_text(json.dumps(data))
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = uninstall_hooks()
    assert rc == 0
    assert "無需移除" in capsys.readouterr().out


def test_uninstall_hooks_file_not_exist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """檔案不存在 → 友善回 0。"""
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)
    rc = uninstall_hooks()
    assert rc == 0
    out = capsys.readouterr().out
    assert "不存在" in out


def test_uninstall_hooks_preserves_other_hooks(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """移除 ring hook 時，其他工具的 hook 保留不動。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    other_hook = {"type": "command", "command": "other-tool run", "timeout": 5}
    ring_hook = {"type": "command", "command": "ring hook", "timeout": 10}
    data = {
        "hooks": {
            "Stop": [{"hooks": [ring_hook, other_hook]}],
        }
    }
    settings.write_text(json.dumps(data))
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    uninstall_hooks()
    result = json.loads(settings.read_text())
    stop_cmds = [h["command"] for g in result["hooks"]["Stop"] for h in g.get("hooks", [])]
    assert "other-tool run" in stop_cmds, "他人 hook 應保留"
    assert "ring hook" not in stop_cmds


def test_uninstall_hooks_dry_run_no_write(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """dry_run 不寫檔。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    _settings_with_ring_hook(settings)
    mtime_before = settings.stat().st_mtime
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    rc = uninstall_hooks(dry_run=True)
    assert rc == 0
    assert settings.stat().st_mtime == mtime_before, "dry_run 不應寫檔"
    assert "dry-run" in capsys.readouterr().out


def test_uninstall_hooks_invalid_json(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """settings.json 非法 JSON → 回 1。"""
    settings = tmp_path / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{invalid")
    monkeypatch.setattr("ring.hook.Path.home", lambda: tmp_path)

    assert uninstall_hooks() == 1


# ---------------------------------------------------------------------------
# waiting_detail：等你時「到底在等什麼」
# ---------------------------------------------------------------------------


def test_permission_prompt_notification_uses_stashed_permission_detail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """裸 PermissionRequest 的指令摘要暫存起來；後續 permission_prompt Notification
    轉 WAITING 時拿它當 waiting_detail，取代籠統的「Claude needs your permission」。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    payload = _bare_permission_request()
    payload["tool_input"] = {"command": "rm -rf node_modules"}
    _feed(monkeypatch, payload)
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WORKING.value
    assert data["pending_permission_detail"] == "Bash: rm -rf node_modules"

    _feed(monkeypatch, _permission_prompt_notification(cwd="/x"))
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_detail"] == "Bash: rm -rf node_modules"
    assert data["waiting_kind"] == "permission"


def test_permission_prompt_notification_after_post_tool_use_falls_back_to_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """PostToolUse 代表權限已放行 → 清掉暫存；之後的 permission_prompt Notification
    屬於新的等待，detail 回到 Notification 自己的籠統訊息。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    payload = _bare_permission_request()
    payload["tool_input"] = {"command": "rm -rf node_modules"}
    _feed(monkeypatch, payload)
    assert hook.run_hook() == 0

    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": "/x"})
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert "pending_permission_detail" not in data

    _feed(monkeypatch, _permission_prompt_notification(cwd="/x"))
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_detail"] == "Claude needs your permission"
    assert data["waiting_kind"] == "permission"


def test_stale_stashed_permission_detail_not_used(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """暫存超過新鮮期（120 秒）→ 不拿來當 waiting_detail，回籠統訊息。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    payload = _bare_permission_request()
    payload["tool_input"] = {"command": "rm -rf node_modules"}
    _feed(monkeypatch, payload)
    assert hook.run_hook() == 0

    # 把暫存時間戳改成遠早於新鮮期（模擬時間流逝，不 patch 時鐘）
    row_path = tmp_path / "s1.json"
    row = json.loads(row_path.read_text())
    row["pending_permission_detail_at"] -= 999.0
    row["pending_permissions"][0]["at"] -= 999.0
    row_path.write_text(json.dumps(row))

    _feed(monkeypatch, _permission_prompt_notification(cwd="/x"))
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_detail"] == "Claude needs your permission"


def test_waiting_detail_captures_question_text(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """AskUserQuestion → 存第一個問題的內容。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "cwd": "/x",
            "tool_name": "AskUserQuestion",
            "tool_input": {"questions": [{"question": "要用哪個 auth 方案？"}]},
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert "要用哪個 auth 方案" in data["waiting_detail"]
    assert data["waiting_detail"].startswith("AskUserQuestion: ")
    assert data["waiting_kind"] == "question"


def test_waiting_kind_detects_plan_approval(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Notification",
            "cwd": "/x",
            "requires_action": True,
            "waiting_for": "plan_approval",
            "message": "Please approve the plan",
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "plan"


def test_waiting_detail_collapses_multiline_and_truncates(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """暫存的指令摘要沿用 _action_detail 的整形：多行壓單行、超長截斷。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    payload = _bare_permission_request()
    payload["tool_input"] = {"command": "echo start\n" + "x" * 400}
    _feed(monkeypatch, payload)
    assert hook.run_hook() == 0

    _feed(monkeypatch, _permission_prompt_notification(cwd="/x"))
    assert hook.run_hook() == 0
    detail = json.loads((tmp_path / "s1.json").read_text())["waiting_detail"]
    assert "\n" not in detail
    assert len(detail) <= 160
    assert detail.endswith("…")


def test_non_waiting_event_has_no_waiting_detail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """🟢 WORKING 的 PreToolUse 就算帶 tool_input 也不寫 waiting_detail（沒在等）。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "PreToolUse",
            "cwd": "/x",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WORKING.value
    assert "waiting_detail" not in data


def test_notification_message_used_as_waiting_detail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Notification",
            "notification_type": "permission_prompt",
            "message": "Claude needs your permission to use Edit",
            "cwd": "/x",
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_detail"] == "Claude needs your permission to use Edit"
    assert data["waiting_kind"] == "permission"


# ---------------------------------------------------------------------------
# codex 核可等待：hook 側寫入 last_event 與暫存 detail（讀取側判定的原料）
# ---------------------------------------------------------------------------


def _codex_bare_permission_request(session_id: str = "thread-1") -> dict[str, Any]:
    """真實 Codex 0.144.4 裸 PermissionRequest 的欄位形狀（取自 hook_payloads.jsonl 實錄）。

    schema 固定欄位（additionalProperties=false）：無 requires_action / waiting_for，
    tool_name / tool_input 必有。
    """
    return {
        "session_id": session_id,
        "turn_id": "019f63f6-5f51-7792-aedf-dedbbcd0251d",
        "transcript_path": f"/nonexistent/rollout-{session_id}.jsonl",
        "cwd": "/repo",
        "hook_event_name": "PermissionRequest",
        "model": "gpt-5.6-sol",
        "permission_mode": "default",
        "tool_name": "Bash",
        "tool_input": {"command": "cp /tmp/fix.py pelicanconf.py", "description": "修 CSS 路徑"},
    }


def test_codex_bare_permission_request_writes_last_event_and_stash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """codex 裸 PermissionRequest → 🟢 working，但 row 要留下讀取側逾時判定的原料：
    last_event（判定「還停在權限請求」）＋ pending_permission_detail（升紅時的 detail）。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, _codex_bare_permission_request())

    assert hook.run_hook(provider="codex") == 0

    data = json.loads((tmp_path / "codex:thread-1.json").read_text())
    assert data["status"] == Status.WORKING.value
    assert data["last_event"] == "PermissionRequest"
    assert data["pending_permission_detail"] == "Bash: cp /tmp/fix.py pelicanconf.py"
    assert "waiting_detail" not in data


def test_codex_subsequent_event_overwrites_last_event_and_clears_stash(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """核可後的 PostToolUse 覆寫 last_event 並清掉暫存 → 讀取側的逾時判定自然失效。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, _codex_bare_permission_request())
    assert hook.run_hook(provider="codex") == 0

    _feed(
        monkeypatch,
        {"session_id": "thread-1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": "/repo"},
    )
    assert hook.run_hook(provider="codex") == 0

    data = json.loads((tmp_path / "codex:thread-1.json").read_text())
    assert data["status"] == Status.WORKING.value
    assert data["last_event"] == "PostToolUse"
    assert "pending_permission_detail" not in data


# ---------------------------------------------------------------------------
# Stop 事件的「回合結尾純文字提問」偵測（question_detect.py）
# ---------------------------------------------------------------------------


def test_stop_trailing_question_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """claude-code Stop、結尾是問句 → 🔴 等你，kind=question，detail 含問句本文。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": "先做了 A。\n\n要不要順便修 B？",
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "question"
    assert "要不要順便修 B？" in data["waiting_detail"]


def test_stop_trailing_statement_writes_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """結尾是陳述句 → 維持 🟡，不誤報。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": "已經修好了，測試都綠了。",
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value
    assert "waiting_kind" not in data


def test_stop_question_mid_message_statement_at_end_writes_idle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """問句在訊息中段、結尾是陳述句 → 不容誤報，維持 🟡。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": "先確認一下：你要 A 還是 B？我會用 A 繼續做。",
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value


def test_stop_missing_transcript_falls_back_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """沒有 last_assistant_message、transcript_path 指向不存在的檔 → 安全退回 🟡，不炸。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "transcript_path": str(tmp_path / "missing.jsonl"),
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value


def test_stop_malformed_transcript_falls_back_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """transcript 檔存在但不是合法 JSONL → 安全退回 🟡，不炸。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("not json at all\n{{{broken\n", encoding="utf-8")
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "transcript_path": str(transcript),
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value


def test_stop_transcript_fallback_detects_trailing_question(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """payload 沒帶 last_assistant_message 時，退回讀 transcript 尾端的 assistant 文字。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(
        '{"type": "assistant", "message": {"content": [{"type": "text", "text": "要繼續嗎？"}]}}\n',
        encoding="utf-8",
    )
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "transcript_path": str(transcript),
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "question"
    assert "要繼續嗎？" in data["waiting_detail"]


def test_codex_stop_trailing_question_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """codex Stop payload 也帶 last_assistant_message（見 hook_payloads.jsonl 實錄），
    結尾是問句時同樣升 🔴，行為與 claude-code 一致。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "thread-1",
            "hook_event_name": "Stop",
            "cwd": "/repo",
            "last_assistant_message": "修完了 footer。要不要一起看看手機版？",
        },
    )
    assert hook.run_hook(provider="codex") == 0
    data = json.loads((tmp_path / "codex:thread-1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "question"
    assert "要不要一起看看手機版？" in data["waiting_detail"]


def test_stop_trailing_question_with_trailing_code_fence(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """問句後面跟著程式碼區塊收尾 → 圍欄先剝掉，還是要偵測到問句。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": "這樣可以嗎？\n\n```bash\nls -la\n```",
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "question"
    assert "這樣可以嗎？" in data["waiting_detail"]


def test_stop_trailing_question_disabled_by_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """detect_stop_questions=false → 就算結尾是問句也維持 🟡。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setattr(hook, "get_config", lambda: Config(detect_stop_questions=False))
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": "要不要順便修 B？",
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value
    assert "waiting_kind" not in data


# ---------------------------------------------------------------------------
# 一般 Notification 不得把既有的 🔴 等你降回 🟡（_notification_keeps_waiting）
# ---------------------------------------------------------------------------


def _idle_prompt(session_id: str = "s1") -> dict[str, Any]:
    """Claude Code 在輸入區閒置滿 60 秒送的通知（取自 hook_payloads.jsonl 實錄形狀）。"""
    return {
        "session_id": session_id,
        "hook_event_name": "Notification",
        "notification_type": "idle_prompt",
        "message": "Claude is waiting for your input",
        "cwd": "/x",
    }


def test_idle_prompt_keeps_stop_question_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Stop 結尾提問升成 🔴 之後，60 秒後的 idle_prompt 不得把它降回 🟡。

    這是「回覆放太久沒動作就自己變成跑完停著」的成因：idle_prompt 的語意是「還在等你」，
    降黃等於把真正需要你的那一列從看板上抹掉。waiting_kind / waiting_detail 一併沿用。
    """
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "Stop",
            "cwd": "/x",
            "last_assistant_message": "先做了 A。\n\n要不要順便修 B？",
        },
    )
    assert hook.run_hook() == 0

    _feed(monkeypatch, _idle_prompt())
    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "question"
    assert "要不要順便修 B？" in data["waiting_detail"]


def test_idle_prompt_keeps_permission_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """權限等待也一樣保紅——閒置提醒不是「使用者核可了」的證據。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "permission_prompt", "cwd": "/x"},
    )
    assert hook.run_hook() == 0

    _feed(monkeypatch, _idle_prompt())
    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_kind"] == "permission"


def test_idle_prompt_does_not_re_notify(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """保住既有 🔴 不算新的等待事件 → 不重發系統通知（重複提醒歸 TUI 排程器）。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    spy = _SpyNotifier()
    monkeypatch.setattr("ring.notify._NOTIFIERS", [spy])
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "permission_prompt", "cwd": "/x"},
    )
    assert hook.run_hook() == 0
    assert len(spy.sent) == 1

    _feed(monkeypatch, _idle_prompt())
    assert hook.run_hook() == 0

    assert len(spy.sent) == 1, "沿用既有 🔴 不該再發一則系統通知"


def test_idle_prompt_still_settles_working_to_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """既有狀態是 🟢 時，idle_prompt 照舊收斂成 🟡——保紅只擋降級，不改其他語意。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Bash", "cwd": "/x"},
    )
    assert hook.run_hook() == 0

    _feed(monkeypatch, _idle_prompt())
    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value


def test_user_reply_still_clears_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """真的回應了（UserPromptSubmit）仍照舊清掉 🔴，保紅不會讓紅色卡住。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "permission_prompt", "cwd": "/x"},
    )
    assert hook.run_hook() == 0
    _feed(monkeypatch, _idle_prompt())
    assert hook.run_hook() == 0

    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "UserPromptSubmit", "cwd": "/x"})
    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WORKING.value
