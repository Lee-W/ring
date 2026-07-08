import io
import json
from pathlib import Path
from typing import Any

import pytest

import ring.hook as hook
from ring.config import Config
from ring.hook import _is_ring_hook_command, install_hooks, uninstall_hooks
from ring.registry import Status


@pytest.fixture(autouse=True)
def _hermetic_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """讓 hook 測試不受機器 config 影響（預設 backend=auto → 不委派 agent-hooks）。

    同時清空 notifier registry：hook 現在會在 WAITING 事件就地發系統通知，darwin 上
    osascript 永遠可用，不擋會在跑測試時噴真實通知。空 registry → _select_notifier 回
    None → notify_waiting no-op。要驗證「有發」的測試自己注入 spy notifier。
    stats 的狀態轉換 log 也導去 tmp，避免測試寫進使用者的 events.jsonl。"""
    monkeypatch.setattr("ring.hook.get_config", lambda: Config())
    monkeypatch.setattr("ring.notify._NOTIFIERS", [])
    monkeypatch.setattr("ring.stats.EVENTS_PATH", tmp_path / "events.jsonl")


def _feed(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _settings_with_ring_hook(settings: Path, cmd: str = "ring hook", timeout: int = hook._HOOK_TIMEOUT) -> None:
    """寫一個已有 ring hook 的 settings.json 到 settings 路徑。"""
    data = {
        "hooks": {e: [{"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]}] for e in hook._HOOK_EVENTS}
    }
    settings.write_text(json.dumps(data, indent=2))


def test_stop_writes_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"})
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value
    assert data["cwd"] == "/x"


def test_hook_event_writes_tmux_binding_and_hook_pid(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    monkeypatch.setenv("TMUX_PANE", "%42")
    monkeypatch.setattr(hook, "_controlling_tty", lambda: "/dev/ttys042")
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"})

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["tmux_pane"] == "%42"
    assert data["tty"] == "/dev/ttys042"
    assert isinstance(data["hook_pid"], int)


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


def test_regular_notification_writes_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {"session_id": "s1", "hook_event_name": "Notification", "notification_type": "auth_success", "cwd": "/x"},
    )

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value


def test_permission_request_writes_waiting(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/x"})

    assert hook.run_hook() == 0

    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value


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
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/proj"})

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
    # payload 帶 tty，避免 _session_tty 去呼叫 ps（那也是 subprocess.run）
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/x", "tty": "/dev/ttys1"})

    assert hook.run_hook() == 0
    assert len(calls) == 1
    assert calls[0][0][:2] == ["agent-hooks", "callback"]
    assert "--provider" in calls[0][0]
    assert calls[0][1] is not None and "PermissionRequest" in calls[0][1]  # 原始 payload 被透傳
    assert json.loads((tmp_path / "s1.json").read_text())["status"] == Status.WAITING.value  # 狀態照寫


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
    data = {"hooks": {e: entry for e in hook._CODEX_HOOK_EVENTS}}
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


def test_waiting_detail_captures_bash_command(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """PermissionRequest 帶 tool_input.command → registry 存 `Bash: <指令>`。"""
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "PermissionRequest",
            "cwd": "/x",
            "tool_name": "Bash",
            "tool_input": {"command": "rm -rf node_modules"},
        },
    )
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.WAITING.value
    assert data["waiting_detail"] == "Bash: rm -rf node_modules"
    assert data["waiting_kind"] == "permission"


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
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    long_cmd = "echo start\n" + "x" * 400
    _feed(
        monkeypatch,
        {
            "session_id": "s1",
            "hook_event_name": "PermissionRequest",
            "cwd": "/x",
            "tool_name": "Bash",
            "tool_input": {"command": long_cmd},
        },
    )
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
