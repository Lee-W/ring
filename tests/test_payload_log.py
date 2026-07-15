"""ring.payload_log——診斷用原始 hook payload 取證 logger（預設關閉）。"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest

import ring.hook as hook
import ring.payload_log as payload_log
from ring.config import Config
from ring.payload_log import maybe_log_raw_payload, payload_log_enabled


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr("ring.hook.get_config", lambda: Config())
    monkeypatch.setattr("ring.notify._NOTIFIERS", [])
    monkeypatch.setattr("ring.stats.EVENTS_PATH", tmp_path / "events.jsonl")


def _feed(monkeypatch: pytest.MonkeyPatch, payload: dict[str, Any]) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))


# ---------------------------------------------------------------------------
# 開關
# ---------------------------------------------------------------------------


def test_disabled_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.config.get_config", lambda: Config(debug_payload_log=False))
    monkeypatch.delenv("RING_DEBUG_PAYLOAD_LOG", raising=False)
    assert payload_log_enabled() is False


def test_env_var_overrides_config_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.config.get_config", lambda: Config(debug_payload_log=False))
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "1")
    assert payload_log_enabled() is True


def test_env_var_overrides_config_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.config.get_config", lambda: Config(debug_payload_log=True))
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "off")
    assert payload_log_enabled() is False


def test_config_key_enables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.config.get_config", lambda: Config(debug_payload_log=True))
    monkeypatch.delenv("RING_DEBUG_PAYLOAD_LOG", raising=False)
    assert payload_log_enabled() is True


# ---------------------------------------------------------------------------
# maybe_log_raw_payload：開啟時寫、關閉時不寫、壞 payload 不炸
# ---------------------------------------------------------------------------


def test_writes_full_raw_payload_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "1")
    p = tmp_path / "hook_payloads.jsonl"
    payload = {
        "session_id": "s1",
        "hook_event_name": "PermissionRequest",
        "cwd": "/x",
        "tool_input": {"nested": {"a": [1, 2, 3]}},
    }
    maybe_log_raw_payload("claude-code", payload, path=p, now=123.0)
    lines = [json.loads(line) for line in p.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["ts"] == 123.0
    assert lines[0]["provider"] == "claude-code"
    assert lines[0]["event"] == "PermissionRequest"
    assert lines[0]["payload"] == payload  # 完整原文，不裁切


def test_does_not_write_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "0")
    p = tmp_path / "hook_payloads.jsonl"
    maybe_log_raw_payload("claude-code", {"session_id": "s1"}, path=p, now=1.0)
    assert not p.exists()


def test_bad_payload_does_not_raise(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """payload 含不可序列化物件時安靜吞掉，不得炸出例外（json.dumps default=str 兜底）。"""
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "1")
    p = tmp_path / "hook_payloads.jsonl"

    class Weird:
        def __repr__(self) -> str:
            return "<weird>"

    maybe_log_raw_payload("claude-code", {"session_id": "s1", "obj": Weird()}, path=p, now=1.0)
    # 不炸；因為 default=str，實際上還是寫得出來
    assert p.exists()
    line = json.loads(p.read_text().splitlines()[0])
    assert "weird" in line["payload"]["obj"]


def test_write_failure_is_swallowed(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """目標路徑不可寫（例如指向一個目錄）時，不得拋例外。"""
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "1")
    p = tmp_path / "is_a_dir.jsonl"
    p.mkdir()
    maybe_log_raw_payload("claude-code", {"session_id": "s1"}, path=p, now=1.0)  # 不炸就算過


def test_get_config_failure_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom() -> Config:
        raise RuntimeError("config 讀壞了")

    monkeypatch.setattr("ring.config.get_config", _boom)
    monkeypatch.delenv("RING_DEBUG_PAYLOAD_LOG", raising=False)
    assert payload_log_enabled() is False


# ---------------------------------------------------------------------------
# 上限砍半
# ---------------------------------------------------------------------------


def test_trims_oversized_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(payload_log, "_MAX_BYTES", 200)
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "1")
    p = tmp_path / "hook_payloads.jsonl"
    for i in range(20):
        maybe_log_raw_payload("claude-code", {"session_id": "s1", "i": i}, path=p, now=float(i))
    lines = [json.loads(line) for line in p.read_text().splitlines()]
    assert len(lines) < 20  # 有砍半過
    assert lines[-1]["payload"]["i"] == 19  # 保留最新


# ---------------------------------------------------------------------------
# 透過 hook.run_hook 端到端驗證（開/關兩態）
# ---------------------------------------------------------------------------


def test_run_hook_writes_raw_payload_when_enabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path / "sessions")
    log_path = tmp_path / "hook_payloads.jsonl"
    monkeypatch.setattr("ring.payload_log.PAYLOAD_LOG_PATH", log_path)
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "1")
    payload = {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/x"}
    _feed(monkeypatch, payload)

    assert hook.run_hook() == 0

    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["payload"] == payload
    assert lines[0]["event"] == "PermissionRequest"


def test_run_hook_skips_raw_payload_when_disabled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(hook, "RING_REGISTRY", tmp_path / "sessions")
    log_path = tmp_path / "hook_payloads.jsonl"
    monkeypatch.setattr("ring.payload_log.PAYLOAD_LOG_PATH", log_path)
    monkeypatch.setenv("RING_DEBUG_PAYLOAD_LOG", "0")
    _feed(monkeypatch, {"session_id": "s1", "hook_event_name": "PermissionRequest", "cwd": "/x"})

    assert hook.run_hook() == 0

    assert not log_path.exists()
