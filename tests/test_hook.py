import io
import json
from pathlib import Path
from typing import Any

import pytest

import ring.hook as hook
from ring.hook import _is_ring_hook_command, install_hooks, uninstall_hooks
from ring.registry import Status


def _feed(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


def _settings_with_ring_hook(settings: Path, cmd: str = "ring hook") -> None:
    """寫一個已有 ring hook 的 settings.json 到 settings 路徑。"""
    data = {"hooks": {e: [{"hooks": [{"type": "command", "command": cmd, "timeout": 10}]}] for e in hook._HOOK_EVENTS}}
    settings.write_text(json.dumps(data, indent=2))


def test_stop_writes_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "Stop", "cwd": "/x"})
    assert hook.run_hook() == 0
    data = json.loads((tmp_path / "s1.json").read_text())
    assert data["status"] == Status.IDLE.value
    assert data["cwd"] == "/x"


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


def test_session_end_deletes_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    (tmp_path / "s2.json").write_text("{}")
    _feed(monkeypatch, {"session_id": "s2", "hook_event_name": "SessionEnd", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert not (tmp_path / "s2.json").exists()


def test_unknown_event_is_ignored(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path)
    _feed(monkeypatch, {"session_id": "s3", "hook_event_name": "PreToolUse", "cwd": "/x"})
    assert hook.run_hook() == 0
    assert not (tmp_path / "s3.json").exists()


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
