import json
import sqlite3
import subprocess
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

import ring.registry as registry
from ring.registry import (
    ACTIVE_WINDOW_SECONDS,
    Session,
    Status,
    TmuxPane,
    _apply_waiting,
    _codex_latest_action,
    _codex_tail_kind,
    _codex_threads,
    _hook_heartbeat_stale,
    _hook_sessions,
    _scan_status,
    _synthetic_sessions,
    _tmux_process_tree_targets,
    delete_session_state,
    hidden_session_ids,
    hide_session,
    running_claude_pids,
    unhide_session,
)
from ring.transcript import (
    _clean_text,
    _conversation_tail_kind,
    _extract_todo,
    _latest_action,
    _tail_records,
    _tool_summary,
)


def test_status_rank_ordering() -> None:
    assert Status.WAITING.rank < Status.WORKING.rank < Status.IDLE.rank < Status.ENDED.rank


def test_scan_status_thresholds() -> None:
    assert _scan_status(10) is Status.WORKING
    assert _scan_status(1000) is Status.IDLE
    assert _scan_status(10**9) is Status.ENDED


def test_clean_text_strips_command_noise() -> None:
    assert _clean_text("<local-command-stdout>Bye!</local-command-stdout>") == ""
    assert _clean_text("  hello\nworld ") == "hello"  # 只取第一行


def test_tool_summary_enriches_common_tools() -> None:
    assert _tool_summary({"name": "Edit", "input": {"file_path": "/a/b/foo.py"}}) == "→ Edit foo.py"
    assert _tool_summary({"name": "Bash", "input": {"command": "git status\n"}}) == "→ Bash: git status"
    assert _tool_summary({"name": "Grep", "input": {"pattern": "TODO"}}) == "→ Grep TODO"
    assert _tool_summary({"name": "WebFetch"}) == "→ WebFetch"  # 沒特例 → 退回工具名
    assert _tool_summary({}) == ""


def test_latest_action_prefers_tool_use_and_skips_noise() -> None:
    records = [
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "old"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Edit"}]}},
        {
            "type": "user",
            "message": {"content": [{"type": "text", "text": "<local-command-stdout>x</local-command-stdout>"}]},
        },
    ]
    assert _latest_action(records) == "→ Edit"


def test_latest_action_falls_back_to_assistant_text() -> None:
    records = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hello there"}]}}]
    assert _latest_action(records) == "hello there"


def test_extract_todo_counts_completed() -> None:
    records = [
        {
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "name": "TodoWrite",
                        "input": {"todos": [{"status": "completed"}, {"status": "completed"}, {"status": "pending"}]},
                    }
                ]
            }
        }
    ]
    assert _extract_todo(records) == (2, 3)


def test_extract_todo_none_when_absent() -> None:
    records = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    assert _extract_todo(records) is None


def test_tail_records_reads_last_valid_json(tmp_path: Path) -> None:
    p = tmp_path / "s.jsonl"
    p.write_text("\n".join(['{"a": 1}', "not json", '{"b": 2}']) + "\n")
    records = _tail_records(p)
    assert records[-1] == {"b": 2}
    assert {"a": 1} in records


def _reset_claude_ps_caches(monkeypatch: pytest.MonkeyPatch) -> None:
    """把 running_claude_pids / background_agent_session_ids 共用的 ps 快取全部歸零。

    三層快取（``_pids_cache``、``_ps_claude_snapshot_cache``、``_bg_agent_session_ids_cache``）
    共用 1 秒 TTL；若只重置其中一層，快速連跑的測試會撈到上一個測試 monkeypatch 的
    ``subprocess.run`` 留下的舊 ps 快照，斷言會對到錯的 fixture（已實測會發生）。
    """
    monkeypatch.setattr("ring.registry._pids_cache", (-1.0, []))
    monkeypatch.setattr("ring.registry._ps_claude_snapshot_cache", (-1.0, ""))
    monkeypatch.setattr("ring.registry._bg_agent_session_ids_cache", (-1.0, frozenset()))


def test_running_claude_pids_ignores_daemon_and_bg_pty(monkeypatch: pytest.MonkeyPatch) -> None:
    """daemon 濾除；bg-pty-host 承載者＋子行程成對出現時，去重成一個 pid，且是子行程。

    子行程 comm 被 daemon-exec 截斷成本機路徑片段（``/Users/me/.l``），靠 args 內的
    claude 安裝路徑標記（``claude/versions/``）辨識，而非 comm/args[0] basename。
    """

    class Result:
        returncode = 0
        stdout = "\n".join(
            [
                "  PID TTY      COMM ARGS",
                (" 101 ??       /Users/me/.local/bin/claude /Users/me/.local/bin/claude daemon run --origin transient"),
                (  # 承載者：bg-pty-host + session-id
                    " 102 ??       claude "
                    "claude --bg-pty-host /tmp/cc-daemon/pty/s.sock 72 35 -- "
                    "/Users/me/.local/share/claude/versions/2.1.187 --session-id abc"
                ),
                (  # 子行程：comm 截斷、同 session-id、無 bg flag，cwd 才誠實
                    " 103 ttys001 /Users/me/.l "
                    "/Users/me/.local/share/claude/versions/2.1.187 --session-id abc "
                    "--resume /Users/me/.claude/projects/x/abc.jsonl --fork-session"
                ),
                " 104 ttys002 claude claude --plugin-dir /work/app",
            ]
        )

    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr("ring.registry.subprocess.run", lambda *args, **kwargs: Result())

    assert running_claude_pids() == [103, 104], "承載者應被子行程換掉，daemon 應濾除"


def test_running_claude_pids_only_carrier_no_child_falls_back_to_carrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """只有承載者、沒有子行程時（fallback），仍保留承載者這個 pid，好過整個 session 消失。"""

    class Result:
        returncode = 0
        stdout = "\n".join(
            [
                "  PID TTY      COMM ARGS",
                (
                    " 201 ttys001 claude "
                    "claude --bg-pty-host /tmp/cc-daemon/pty/s.sock 72 35 -- "
                    "/Users/me/.local/share/claude/versions/2.1.187 --session-id xyz"
                ),
            ]
        )

    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr("ring.registry.subprocess.run", lambda *args, **kwargs: Result())

    assert running_claude_pids() == [201]


def test_running_claude_pids_ignores_bg_pty_host_without_session_id(monkeypatch: pytest.MonkeyPatch) -> None:
    """``--bg-pty-host`` 指向 spare sock、無 ``--session-id``＝暖機承載者，仍要濾除。"""

    class Result:
        returncode = 0
        stdout = "\n".join(
            [
                "  PID TTY      COMM ARGS",
                (" 301 ??       claude claude --bg-pty-host /tmp/cc-daemon/spare/x.sock 72 35"),
                " 302 ttys001 claude claude --plugin-dir /work/app",
            ]
        )

    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr("ring.registry.subprocess.run", lambda *args, **kwargs: Result())

    assert running_claude_pids() == [302]


def test_running_claude_pids_ignores_bg_spare(monkeypatch: pytest.MonkeyPatch) -> None:
    """``claude ... --bg-spare <token>`` 是 Claude Code 預熱的備用 process，不是使用者 session。

    token 形狀（``--bg-spare`` 為 ``--`` 前綴 flag，非位置參數）取自本機 claude CLI 2.1.206
    二進位 strings 掃描：spawn 呼叫 ``[...,"--bg-pty-host",r,"200","50","--",...,"--bg-spare",n]``，
    以及該 process 自己對 argv 做的 ``e.includes("--bg-spare", t+1)`` 檢查。
    """

    class Result:
        returncode = 0
        stdout = "\n".join(
            [
                "  PID TTY      COMM ARGS",
                (" 104 ??       claude /Users/me/.local/share/claude/versions/2.1.206 --bg-spare tok123"),
                " 105 ttys001 claude claude --plugin-dir /work/app",
            ]
        )

    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr("ring.registry.subprocess.run", lambda *args, **kwargs: Result())

    assert running_claude_pids() == [105]


def test_is_claude_session_line_recognizes_truncated_comm_via_path_marker() -> None:
    """comm 被截斷（daemon-exec）時，靠 args 的 claude 安裝路徑標記辨識，不靠 comm/args[0]。"""
    assert registry._is_claude_session_line("claude", "claude --plugin-dir /work/app") is True
    # comm 截斷、args[0] 只是版本號（不是 "claude"）→ 靠 ClaudeCode.app / claude/versions/ 標記
    assert (
        registry._is_claude_session_line(
            "/Users/weilee/.l",
            "/Applications/Claude Code.app/Contents/Resources/ClaudeCode.app/2.1.187 --session-id abc",
        )
        is True
    )
    assert (
        registry._is_claude_session_line(
            "/Users/weilee/.l", "/Users/me/.local/share/claude/versions/2.1.187 --session-id abc"
        )
        is True
    )
    # 無 claude 相關線索 → 不是
    assert registry._is_claude_session_line("/Users/weilee/.l", "2.1.187 --session-id abc") is False
    assert registry._is_claude_session_line("zsh", "-zsh") is False


def test_is_claude_session_line_third_branch_requires_session_id() -> None:
    """第三分支（basename token fallback）必須同時帶 --session-id，否則誤判 grep/less 等無關 process。"""
    assert registry._is_claude_session_line("grep", "grep -r claude .") is False
    assert registry._is_claude_session_line("less", "less claude") is False
    assert registry._is_claude_session_line("/Users/x/.l", "/opt/homebrew/bin/claude --session-id abc") is True


def test_arg_session_id_parses_token_after_flag() -> None:
    assert registry._arg_session_id("claude --bg-pty-host x.sock --session-id abc --fork-session") == "abc"
    assert registry._arg_session_id("claude --plugin-dir /work/app") is None
    assert registry._arg_session_id("claude --session-id") is None  # flag 是最後一個 token，沒有值


def test_is_claude_background_process_four_branches() -> None:
    """取捨 B 的四分支：daemon run／bg-spare／bg-pty-host 無 session-id → True；其餘 False。"""
    assert registry._is_claude_background_process("claude daemon run --origin transient") is True
    assert registry._is_claude_background_process("claude --bg-spare tok123") is True
    assert registry._is_claude_background_process("claude --bg-pty-host /tmp/spare/x.sock 72 35") is True
    assert (
        registry._is_claude_background_process("claude --bg-pty-host /tmp/pty/x.sock 72 35 --session-id abc") is False
    )
    assert registry._is_claude_background_process("claude --plugin-dir /work/app") is False


def test_background_agent_session_ids_only_collects_bg_pty_host_with_session_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """真背景 agent（bg-pty-host + session-id）的 session-id 入集合；spare/daemon/前景都不入。"""

    class Result:
        returncode = 0
        stdout = "\n".join(
            [
                "  PID TTY      COMM ARGS",
                " 101 ??       claude claude daemon run --origin transient",
                " 102 ??       claude claude --bg-pty-host /tmp/spare/x.sock 72 35",  # 暖機，無 session-id
                (
                    " 103 ttys001 claude claude --bg-pty-host /tmp/pty/s.sock 72 35 -- "
                    "/Users/me/.local/share/claude/versions/2.1.187 --session-id abc"
                ),
                " 104 ttys002 claude claude --plugin-dir /work/app",  # 前景，無 session-id
            ]
        )

    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr("ring.registry.subprocess.run", lambda *args, **kwargs: Result())

    assert registry.background_agent_session_ids() == {"abc"}


def test_running_codex_pids_excludes_internal_app_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """sandbox / app-server 共用 codex binary，但不能各自復活一筆舊 thread。"""
    ps = "\n".join(
        [
            "101 ttys001 codex codex",
            "102 ttys002 codex codex resume thread-id",
            "201 ttys003 codex /Applications/Codex.app/Resources/codex sandbox -c policy -- command",
            "202 ttys004 codex /Applications/Codex.app/Resources/codex app-server --listen stdio://",
        ]
    )
    monkeypatch.setattr(registry, "_codex_pids_cache", (-1.0, []))
    monkeypatch.setattr(registry, "_ps_codex_snapshot_cache", (-1.0, ""))
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: SimpleNamespace(stdout=ps, returncode=0))

    assert registry.running_codex_pids() == [101, 102]


def test_running_foreground_claude_pids_excludes_background_agent_children(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """背景承載者與它的子行程都不能拿來替前景 hook row 證明存活。"""
    snapshot = "\n".join(
        [
            "  PID COMM ARGS",
            " 101 claude claude --session-id foreground",
            " 201 claude claude --bg-pty-host /tmp/pty/a.sock --session-id agent-a",
            " 202 claude claude --session-id agent-a",
        ]
    )
    monkeypatch.setattr(registry, "_ps_claude_snapshot", lambda: snapshot)
    monkeypatch.setattr(registry, "running_claude_pids", lambda: [101, 202])
    monkeypatch.setattr(registry, "background_agent_session_ids", lambda: {"agent-a"})

    assert registry.running_foreground_claude_pids() == [101]


def test_pids_cwd_batches_live_pids_and_skips_dead_ones(monkeypatch: pytest.MonkeyPatch) -> None:
    """PoC 已驗證：macOS lsof 對批次呼叫裡混了死 pid 時，整體 exit code 仍是 1，但存活
    pid 的區段照樣完整輸出。``_pids_cwd`` 刻意不看 ``returncode``，只解析 stdout；死掉的
    pid 沒出現在輸出裡＝那個 pid 不存在（真資訊），不是「這輪查不到、未知」。
    """
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> Any:
        calls.append(cmd)
        # pid 22 已死：lsof 只回得出 11 的區段，returncode 仍非 0（PoC 實測行為）。
        return SimpleNamespace(stdout="p11\nfcwd\nn/work/eleven\n", returncode=1)

    monkeypatch.setattr("ring.registry.subprocess.run", fake_run)

    result = registry._pids_cwd([11, 22])

    assert result == {11: "/work/eleven"}, "死 pid 不進回傳 dict，不是回傳未知"
    assert len(calls) == 1, f"多個 pid 應合併成一次 lsof 呼叫，實際呼叫 {len(calls)} 次"
    assert calls[0][:3] == ["lsof", "-a", "-p"]
    assert calls[0][3] == "11,22", "多個 pid 應以逗號合併成單一 -p 參數，不是逐 pid 各開一次"


def test_pids_cwd_returns_none_on_subprocess_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """批次呼叫本身失敗（逾時／例外）＝這輪整批未知，不能回空 dict 跟「查到但沒有 cwd」混淆。"""
    monkeypatch.setattr(
        "ring.registry.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="lsof", timeout=3)),
    )

    assert registry._pids_cwd([11, 22]) is None


def test_pids_cwd_empty_pid_list_skips_subprocess_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """沒有活著的 pid 時，不必為了問「零個 pid 的 cwd」白開一次 lsof。"""
    calls = {"n": 0}

    def fake_run(*_args: object, **_kwargs: object) -> Any:
        calls["n"] += 1
        raise AssertionError("pid 清單為空時不該呼叫 subprocess.run")

    monkeypatch.setattr("ring.registry.subprocess.run", fake_run)

    assert registry._pids_cwd([]) == {}
    assert calls["n"] == 0


def test_claude_procs_issues_one_ps_and_one_lsof_call_regardless_of_pid_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """效能回歸測試：修法前 ``_claude_procs()`` 對每個 pid 各開一次 lsof + 一次 ps，
    subprocess 呼叫次數隨 session 數線性成長；修法後固定是 1 次 ps 快照（給 pid 清單、
    背景 agent 判定、tty 共用）＋ 1 次批次 lsof（給 cwd），不隨 pid 數增加。拿掉批次化
    改回逐 pid 呼叫，這個測試會因為 ps/lsof 呼叫次數 > 1 而變紅。
    """
    _reset_claude_ps_caches(monkeypatch)

    ps_snapshot = "\n".join(
        [
            "  PID TTY      COMM ARGS",
            " 101 ttys001 claude claude --session-id s1",
            " 102 ttys002 claude claude --session-id s2",
            " 103 ttys003 claude claude --session-id s3",
        ]
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> Any:
        calls.append(cmd)
        if cmd[0] == "ps":
            return SimpleNamespace(stdout=ps_snapshot, returncode=0)
        if cmd[0] == "lsof":
            out = "\n".join(
                ["p101", "fcwd", "n/work/one", "p102", "fcwd", "n/work/two", "p103", "fcwd", "n/work/three"]
            )
            return SimpleNamespace(stdout=out, returncode=0)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("ring.registry.subprocess.run", fake_run)

    procs = registry._claude_procs()

    assert procs == [
        ("/work/one", "/dev/ttys001"),
        ("/work/two", "/dev/ttys002"),
        ("/work/three", "/dev/ttys003"),
    ]
    ps_calls = [c for c in calls if c[0] == "ps"]
    lsof_calls = [c for c in calls if c[0] == "lsof"]
    assert len(ps_calls) == 1, f"ps 應只呼叫 1 次（3 個 pid 共用同一份快照），實際 {len(ps_calls)} 次"
    assert len(lsof_calls) == 1, f"lsof 應只呼叫 1 次（批次查全部 pid 的 cwd），實際 {len(lsof_calls)} 次"


def test_codex_procs_issues_one_ps_and_one_lsof_call_regardless_of_pid_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``_codex_procs()`` 版本的同一條效能回歸測試，見 ``_claude_procs`` 對應測試說明。"""
    monkeypatch.setattr(registry, "_codex_pids_cache", (-1.0, []))
    monkeypatch.setattr(registry, "_ps_codex_snapshot_cache", (-1.0, ""))

    ps_snapshot = "\n".join(
        [
            "201 ttys011 codex codex",
            "202 ttys012 codex codex",
        ]
    )
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> Any:
        calls.append(cmd)
        if cmd[0] == "ps":
            return SimpleNamespace(stdout=ps_snapshot, returncode=0)
        if cmd[0] == "lsof":
            out = "\n".join(["p201", "fcwd", "n/work/alpha", "p202", "fcwd", "n/work/beta"])
            return SimpleNamespace(stdout=out, returncode=0)
        raise AssertionError(f"unexpected subprocess call: {cmd}")

    monkeypatch.setattr("ring.registry.subprocess.run", fake_run)

    procs = registry._codex_procs()

    assert procs == [("/work/alpha", "/dev/ttys011"), ("/work/beta", "/dev/ttys012")]
    ps_calls = [c for c in calls if c[0] == "ps"]
    lsof_calls = [c for c in calls if c[0] == "lsof"]
    assert len(ps_calls) == 1, f"ps 應只呼叫 1 次，實際 {len(ps_calls)} 次"
    assert len(lsof_calls) == 1, f"lsof 應只呼叫 1 次，實際 {len(lsof_calls)} 次"


def test_delete_session_state_removes_hook_registry_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "sessions"
    registry_dir.mkdir()
    target = registry_dir / "codex:thread-1.json"
    target.write_text(
        json.dumps({"session_id": "codex:thread-1", "provider": "codex", "cwd": "/work"}),
        encoding="utf-8",
    )

    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    assert delete_session_state("codex:thread-1") is True
    assert not target.exists()
    assert delete_session_state("codex:thread-1") is False


def test_delete_session_state_falls_back_to_file_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    registry_dir = tmp_path / "sessions"
    registry_dir.mkdir()
    target = registry_dir / "legacy-name.json"
    target.write_text(
        json.dumps({"session_id": "raw/id", "provider": "claude-code", "cwd": "/work"}),
        encoding="utf-8",
    )

    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    assert delete_session_state("raw/id") is True
    assert not target.exists()


def test_hide_and_unhide_session_ids(tmp_path: Path) -> None:
    path = tmp_path / "deleted_sessions.json"

    assert hidden_session_ids(path=path) == set()

    hide_session("s1", path=path)
    hide_session("s2", path=path)
    hide_session("s1", path=path)
    assert hidden_session_ids(path=path) == {"s1", "s2"}

    unhide_session("s1", path=path)
    assert hidden_session_ids(path=path) == {"s2"}


def test_hide_session_writes_iso_timestamp(tmp_path: Path) -> None:
    path = tmp_path / "deleted_sessions.json"

    before = time.time()
    hide_session("s1", path=path)
    after = time.time()

    raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    hidden_at = datetime.fromisoformat(raw["s1"]).timestamp()
    assert before - 1 <= hidden_at <= after + 1

    hidden = registry.hidden_sessions(path=path)
    assert hidden["s1"] == pytest.approx(hidden_at)


def test_hidden_sessions_migrates_legacy_list_format(tmp_path: Path) -> None:
    path = tmp_path / "deleted_sessions.json"
    path.write_text(json.dumps(["legacy-1", "legacy-2"]), encoding="utf-8")

    hidden = registry.hidden_sessions(path=path)

    assert set(hidden.keys()) == {"legacy-1", "legacy-2"}
    # 遷移後仍視為隱藏中：hidden_session_ids 不 crash、不掉資料。
    assert hidden_session_ids(path=path) == {"legacy-1", "legacy-2"}

    # 檔案已就地寫回新格式（dict，value 是 ISO timestamp）。
    migrated_raw = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(migrated_raw, dict)
    for iso in migrated_raw.values():
        datetime.fromisoformat(iso)  # 不丟例外即為合法 ISO


def test_prune_hidden_sessions_removes_not_found_and_too_old(tmp_path: Path) -> None:
    path = tmp_path / "deleted_sessions.json"
    now = time.time()
    path.write_text(
        json.dumps(
            {
                "keep-1": datetime.fromtimestamp(now - 10, UTC).isoformat(),  # 找得到、夠新 → 留
                "gone-1": datetime.fromtimestamp(now - 10, UTC).isoformat(),  # 找不到 → 清
                "old-1": datetime.fromtimestamp(now - 1000, UTC).isoformat(),  # 找得到但太舊 → 清
            }
        ),
        encoding="utf-8",
    )

    removed = registry.prune_hidden_sessions(known_ids={"keep-1", "old-1"}, older_than=100, now=now, path=path)

    assert set(removed.keys()) == {"gone-1", "old-1"}
    assert registry.hidden_session_ids(path=path) == {"keep-1"}


def test_hide_session_survives_concurrent_writes(tmp_path: Path) -> None:
    """多個 thread（模擬多 process）同時 hide 不同 session，鎖要擋住 lost-update。"""
    path = tmp_path / "deleted_sessions.json"
    session_ids = [f"s{i}" for i in range(16)]

    threads = [threading.Thread(target=hide_session, args=(sid,), kwargs={"path": path}) for sid in session_ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert hidden_session_ids(path=path) == set(session_ids)


# ---------------------------------------------------------------------------
# _conversation_tail_kind (Test plan A)
# ---------------------------------------------------------------------------

_END_TURN_RECORD = {
    "type": "assistant",
    "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "done"}]},
}

_TOOL_USE_RECORD = {
    "type": "assistant",
    "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]},
}

_USER_TOOL_USE_RESULT_FIELD = {
    "type": "user",
    "toolUseResult": {"output": "ok"},
    "message": {"content": []},
}

_USER_TOOL_RESULT_BLOCK = {
    "type": "user",
    "message": {"content": [{"type": "tool_result", "tool_use_id": "x", "content": "ok"}]},
}

_USER_REAL_PROMPT = {
    "type": "user",
    "message": {"content": [{"type": "text", "text": "do X"}]},
}


@pytest.mark.parametrize(
    "records,expected",
    [
        ([_END_TURN_RECORD], "waiting"),
        ([_TOOL_USE_RECORD], "interrupted"),
        ([_USER_TOOL_USE_RESULT_FIELD], "interrupted"),
        ([_USER_TOOL_RESULT_BLOCK], "interrupted"),
        ([_USER_REAL_PROMPT], "working"),
    ],
    ids=[
        "end_turn_is_waiting",
        "tool_use_is_interrupted",
        "user_toolUseResult_field_is_interrupted",
        "user_tool_result_block_is_interrupted",
        "real_prompt_is_working",
    ],
)
def test_tail_kind_basic(records: list[dict[str, Any]], expected: str) -> None:
    assert _conversation_tail_kind(records) == expected


def test_tail_kind_skips_noise_to_find_end_turn() -> None:
    """尾巴全是「跳過型」噪音，仍要往回走到 end_turn → waiting。
    注意：尾巴只放 type 非 user/assistant、isMeta、command 噪音——不混入 tool_result（那是 interrupted）。
    """
    noise_type: dict[str, Any] = {"type": "file-history-snapshot", "cwd": "/foo"}
    noise_system: dict[str, Any] = {"type": "system"}
    noise_meta: dict[str, Any] = {
        "type": "user",
        "isMeta": True,
        "message": {"content": [{"type": "text", "text": "meta"}]},
    }
    noise_cmd: dict[str, Any] = {
        "type": "user",
        "message": {"content": [{"type": "text", "text": "<local-command-stdout>x</local-command-stdout>"}]},
    }
    records: list[dict[str, Any]] = [_END_TURN_RECORD, noise_type, noise_system, noise_meta, noise_cmd]
    assert _conversation_tail_kind(records) == "waiting"


def test_tail_kind_filters_imeta() -> None:
    """isMeta user 要濾：後接 end_turn → 跳過 isMeta → waiting。"""
    imeta: dict[str, Any] = {"type": "user", "isMeta": True, "message": {"content": [{"type": "text", "text": "x"}]}}
    records: list[dict[str, Any]] = [_END_TURN_RECORD, imeta]
    assert _conversation_tail_kind(records) == "waiting"


def test_tail_kind_filters_isidechain() -> None:
    """isSidechain 要濾：sub-agent 旁支不影響主對話判定。"""
    sidechain: dict[str, Any] = {
        "type": "assistant",
        "isSidechain": True,
        "message": {"stop_reason": "tool_use", "content": [{"type": "tool_use", "name": "Bash"}]},
    }
    records: list[dict[str, Any]] = [_END_TURN_RECORD, sidechain]
    assert _conversation_tail_kind(records) == "waiting"


def test_tail_kind_empty_records_is_none() -> None:
    assert _conversation_tail_kind([]) == "none"


def test_tail_kind_only_noise_records_is_none() -> None:
    records = [{"type": "system"}, {"type": "file-history-snapshot"}]
    assert _conversation_tail_kind(records) == "none"


# ---------------------------------------------------------------------------
# _apply_waiting (Test plan B)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status,age,tail_kind,window,expected",
    [
        (Status.IDLE, 600, "waiting", 1800, Status.IDLE),
        (Status.IDLE, 3600, "waiting", 1800, Status.IDLE),
        (Status.IDLE, 600, "interrupted", 1800, Status.IDLE),
        (Status.IDLE, 600, "none", 1800, Status.IDLE),
        (Status.WORKING, 10, "waiting", 1800, Status.IDLE),
        (Status.ENDED, 100, "waiting", 1800, Status.ENDED),
    ],
    ids=[
        "idle_within_window_stays_idle",
        "idle_beyond_window_stays_idle",
        "interrupted_tail_stays_idle",
        "none_tail_stays_idle",
        "working_turn_complete_becomes_idle",
        "ended_status_not_upgraded",
    ],
)
def test_apply_waiting(status: Status, age: int, tail_kind: str, window: int, expected: Status) -> None:
    assert _apply_waiting(status, age, tail_kind, window) is expected


# ---------------------------------------------------------------------------
# hook heartbeat stale detection
# ---------------------------------------------------------------------------


def test_hook_heartbeat_stale_requires_newer_source_file(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    source.write_text("new activity\n")
    heartbeat_at = source.stat().st_mtime - 120

    assert _hook_heartbeat_stale(str(source), heartbeat_at, Status.WAITING, grace_seconds=60)


def test_hook_heartbeat_stale_does_not_flag_long_task_without_source_update(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    source.write_text("old activity\n")
    heartbeat_at = source.stat().st_mtime + 300

    assert not _hook_heartbeat_stale(str(source), heartbeat_at, Status.WORKING, grace_seconds=60)


# ---------------------------------------------------------------------------
# tmux process-tree target disambiguation
# ---------------------------------------------------------------------------


def test_tmux_process_tree_targets_disambiguates_same_cwd_scan_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sessions = [
        Session("session-a", "/work/app", Status.WORKING, 100.0, "—", "scan"),
        Session("session-b", "/work/app", Status.WORKING, 90.0, "—", "scan"),
    ]
    panes = [
        TmuxPane("%1", "/work/app", "main:1.0", pane_pid=10),
        TmuxPane("%2", "/work/app", "main:1.1", pane_pid=20),
    ]
    rows = {
        10: (1, "zsh"),
        11: (10, "claude --resume session-a"),
        20: (1, "zsh"),
        21: (20, "claude --resume session-b"),
    }
    monkeypatch.setattr(registry, "_tmux_panes", lambda: panes)
    monkeypatch.setattr(registry, "_process_rows", lambda: rows)

    assert _tmux_process_tree_targets(sessions) == {
        "session-a": "main:1.0",
        "session-b": "main:1.1",
    }


# ---------------------------------------------------------------------------
# _synthetic_sessions (Test plan A)
# ---------------------------------------------------------------------------


def _make_session(cwd: str) -> Session:
    """建立最小 Session，只填 cwd，其餘用預設值。"""
    return Session(
        session_id=f"scan:{cwd}",
        cwd=cwd,
        status=Status.IDLE,
        last_active=0.0,
        last_action="—",
        source="scan",
    )


def test_synthetic_sessions_basic_one_row() -> None:
    """procs 有一個 cwd、existing 空 → 補一列，各欄位正確。"""
    result = _synthetic_sessions([("/a", "/dev/ttys1")], [])
    assert len(result) == 1
    s = result[0]
    assert s.cwd == "/a"
    assert s.status is Status.IDLE
    assert s.source == "proc"
    assert s.tty == "/dev/ttys1"
    assert s.session_id == "synthetic:/a"


def test_synthetic_sessions_skips_existing_cwd() -> None:
    """existing 已含 cwd=/a 的 Session → 回 []，不補。"""
    existing = [_make_session("/a")]
    result = _synthetic_sessions([("/a", "/dev/ttys1")], existing)
    assert result == []


def test_synthetic_sessions_dedup_same_cwd_takes_first_nonnull_tty() -> None:
    """同 cwd 兩筆 procs（第一筆 tty 空、第二筆有 tty）→ 只回一列，tty 取第一個非空。"""
    result = _synthetic_sessions([("/b", ""), ("/b", "/dev/ttys2")], [])
    assert len(result) == 1
    assert result[0].cwd == "/b"
    assert result[0].tty == "/dev/ttys2"


def test_synthetic_sessions_skips_empty_cwd() -> None:
    """空 cwd 的 proc → 跳過，不生列。"""
    result = _synthetic_sessions([("", "")], [])
    assert result == []


@pytest.mark.parametrize(
    "procs,n_existing_cwds,expected_len",
    [
        ([("/x", ""), ("/y", "/dev/ttys3")], 0, 2),  # 兩個不同 cwd，都不在 existing → 補兩列
        ([("/x", ""), ("/y", "/dev/ttys3")], 1, 1),  # 第一個 cwd 在 existing → 只補一列
        ([("/x", ""), ("", "")], 0, 1),  # 空 cwd 跳過 → 只補 /x
    ],
    ids=["two_cwds_both_new", "two_cwds_one_existing", "one_valid_one_empty"],
)
def test_synthetic_sessions_count(procs: list[tuple[str, str]], n_existing_cwds: int, expected_len: int) -> None:
    cwds = [cwd for cwd, _ in procs if cwd][:n_existing_cwds]
    existing = [_make_session(c) for c in cwds]
    result = _synthetic_sessions(procs, existing)
    assert len(result) == expected_len


# ---------------------------------------------------------------------------
# _hook_sessions stale row cleanup
# ---------------------------------------------------------------------------


def _write_hook_session(
    registry_dir: Path,
    sid: str,
    cwd: str,
    tty: str,
    provider: str = "claude-code",
    last_active: float = 123.0,
) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / f"{sid}.json").write_text(
        json.dumps(
            {
                "session_id": sid,
                "provider": provider,
                "cwd": cwd,
                "status": "waiting",
                "last_active": last_active,
                "last_action": "—",
                "tty": tty,
            }
        )
    )


def test_hook_sessions_ends_stale_tty_even_when_same_cwd_has_live_proc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同 cwd 仍有 live claude 時，舊 hook row 也要用 tty 判斷是否已離場。

    使用者關掉一個 session，但同專案另有 session 還活著時，若只用 cwd 判斷，
    舊 row 會繼續顯示且跳轉失敗。hook 有 tty 時應以 tty 精準排除 stale row。
    """
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "stale", "/work/app", "/dev/ttys001")
    _write_hook_session(registry_dir, "live", "/work/app", "/dev/ttys002")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    by_id = {s.session_id: s for s in _hook_sessions([("/work/app", "/dev/ttys002")])}

    assert by_id["stale"].status is Status.ENDED
    assert by_id["live"].status is Status.WAITING


def test_hook_sessions_keeps_live_session_when_cwd_moved_to_subdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """使用者在 session 裡 cd 進子目錄後，hook payload 的 cwd 變成子目錄，但 claude
    process 實際 cwd（lsof 量到的）仍停在啟動目錄——子目錄底下量不到 live process，
    不該因此被判離場；祖先目錄有活 process 時要保守判定「還活著」。
    """
    project = tmp_path / "work" / "app"
    sub = project / "sub" / "dir"
    sub.mkdir(parents=True)

    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "alive", str(sub), "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    # live proc 的 cwd 停在啟動目錄（祖先），不是 hook 記的子目錄
    sessions = _hook_sessions([(str(project), "/dev/ttys001")])

    assert sessions[0].status is Status.WAITING, "cd 進子目錄不該讓活著的 session 被判離場"


def test_hook_sessions_ends_session_when_live_proc_cwd_is_unrelated_lookalike(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """祖先目錄比對不可用裸字串 startswith：``/work/app`` 不該誤判命中 ``/work/app-other``
    底下的 row（兩者只是前綴字面相似，不是真正的父子目錄關係）。
    """
    project = tmp_path / "work" / "app"
    lookalike_sub = tmp_path / "work" / "app-other" / "sub"
    lookalike_sub.mkdir(parents=True)
    project.mkdir(parents=True)

    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "gone", str(lookalike_sub), "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    # 唯一 live proc 的 cwd 是字面相似但非祖先的目錄
    sessions = _hook_sessions([(str(project), "/dev/ttys001")])

    assert sessions[0].status is Status.ENDED, "字面相似但非祖先目錄不該被誤判為活著"


def test_hook_sessions_matches_live_proc_through_symlink(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """hook cwd 是 symlink 路徑、live proc cwd 是 realpath 時，仍要對得上、不誤判離場。

    lsof 回報解析後的真實路徑，hook 記的是字面路徑；沒有 realpath 正規化，symlink
    專案的活著 session 會因 counts 對不上而被標 ENDED。
    """
    real = tmp_path / "real-proj"
    real.mkdir()
    link = tmp_path / "link-proj"
    link.symlink_to(real)

    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "live", str(link), "")  # hook 記 symlink 路徑
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    # live proc 的 cwd 是 realpath（lsof 風格）
    sessions = _hook_sessions([(str(real), "/dev/ttys009")])

    assert sessions[0].status is Status.WAITING, "symlink 路徑下活著的 session 不該被誤判離場"


def test_hook_sessions_keeps_cwd_fallback_when_live_tty_unknown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """live proc 有 cwd 但 tty 取不到時，保留既有 cwd fallback，避免誤殺。"""
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "maybe-live", "/work/app", "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    sessions = _hook_sessions([("/work/app", "")])

    assert sessions[0].status is Status.WAITING


def test_hook_sessions_keeps_lone_live_session_with_wrong_tty(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """單一 live session 但 hook 記的 tty 不對（被重配 / 錯置）→ 仍要顯示，不可憑空消失。

    cwd 只有一個 live proc、只有一筆 hook row 時，就算 tty 對不上也不該標離場——
    hook 的 tty 不可靠，拿它隱藏唯一活著的 session 是這次「session 不見了」的元兇。
    """
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "alive", "/work/app", "/dev/ttys003")  # 記了錯的 tty
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    sessions = _hook_sessions([("/work/app", "/dev/ttys006")])  # 實際 live tty 不同

    assert sessions[0].status is Status.WAITING, "唯一活著的 session 不該因 tty 對不上而消失"


def test_hook_sessions_caps_same_cwd_same_tty_rows_to_live_process_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """同一終端分頁連開多個 Codex session 時，舊 row 會共用 tty；只保留最新 live_n 筆。"""
    registry_dir = tmp_path / "sessions"
    _write_hook_session(
        registry_dir,
        "codex:old",
        "/work/app",
        "/dev/ttys004",
        provider="codex",
        last_active=100.0,
    )
    _write_hook_session(
        registry_dir,
        "codex:new",
        "/work/app",
        "/dev/ttys004",
        provider="codex",
        last_active=200.0,
    )
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    by_id = {
        s.session_id: s
        for s in _hook_sessions(
            procs_by_provider={
                "claude-code": [],
                "codex": [("/work/app", "/dev/ttys004")],
            }
        )
    }

    assert by_id["codex:old"].status is Status.ENDED
    assert by_id["codex:new"].status is Status.WAITING


def test_hook_sessions_len_leq_live_n_still_cross_checks_tty_when_multiple_rows(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """row 數 <= live 數的「fail-open」分支，row 數 > 1 時仍要用 tty 交叉比對。

    症狀 1 次要成因反例：同 cwd 有一筆真正已 crash 的舊 hook row，剛好又有跟 RiNG hook
    無關的 live process 佔掉同一 cwd 名額，計數「巧合」對上（row 數 == live 數）。
    修復前這個分支完全跳過驗證，crash 掉的 row 永遠不會被判 ENDED；修復後 row 數 > 1
    時仍比照 row 數 > live 數分支做 tty 交叉比對，tty 對不上的那筆才會被標離場。
    """
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "crashed", "/work/app", "/dev/ttys001")  # 舊 tty，已 crash
    _write_hook_session(registry_dir, "alive", "/work/app", "/dev/ttys002")  # tty 對得上活著的 proc
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    # 2 筆 row、2 個 live proc（巧合對上），但只有一個 tty 真的對得上其中一筆 row。
    by_id = {s.session_id: s for s in _hook_sessions([("/work/app", "/dev/ttys002"), ("/work/app", "/dev/ttys003")])}

    assert by_id["crashed"].status is Status.ENDED, "tty 對不上任何活著 proc 的舊 row 該被判離場"
    assert by_id["alive"].status is Status.WAITING, "tty 對得上活著 proc 的 row 不該被誤殺"


def test_hook_sessions_purges_session_start_source_phantom(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """provider 是 SessionStart source（startup 等）的腐壞檔 → 不顯示且自我刪除。"""
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "startup:abc", "/work/app", "", provider="startup")
    _write_hook_session(registry_dir, "real", "/work/app", "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    sessions = _hook_sessions([("/work/app", "/dev/ttys001")])

    ids = {s.session_id for s in sessions}
    assert "startup:abc" not in ids, "幽靈列不該出現"
    assert "real" in ids
    assert not (registry_dir / "startup:abc.json").exists(), "腐壞檔應被刪除（自我修復）"


def test_hook_sessions_liveness_works_for_any_registered_provider(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """泛用：註冊一個全新 provider 的偵測器後，它的 session 也走 process-based 存活清理。"""
    import ring.registry as registry

    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "g1", "/work/app", "", provider="gemini")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)
    monkeypatch.setitem(registry._PROVIDER_PROCS, "gemini", list)  # 有偵測器、但回空（沒 live proc）

    sessions = _hook_sessions(procs_by_provider={"gemini": []})

    assert sessions[0].status is Status.ENDED, "已註冊 provider 無 live proc → 標離場"


def test_hook_sessions_unregistered_provider_fails_open(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """泛用：沒註冊偵測器的 provider → 不靠 process 判離場（fail-open），保留原狀態。"""
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "x1", "/work/app", "", provider="brand-new-tool")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    sessions = _hook_sessions(procs_by_provider={"claude-code": [], "codex": []})

    assert sessions[0].status is Status.WAITING, "無偵測器的新 provider 不該被 process 判離場"


def test_hook_sessions_cleanup_is_provider_specific(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """同 cwd 有 live Claude，不代表 Codex hook row 還活著。"""
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "codex:stale", "/work/app", "", provider="codex")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    sessions = _hook_sessions(
        procs_by_provider={
            "claude-code": [("/work/app", "/dev/ttys002")],
            "codex": [],
        }
    )

    assert sessions[0].status is Status.ENDED


def test_hook_sessions_keeps_status_when_provider_scan_unknown(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``procs_by_provider`` 某 provider 值為 ``None``（這輪掃描失敗／未知）時，該 provider
    底下任何一筆 hook row 都不該被判 ENDED——維持既有狀態，這是本次修的核心行為，
    直接對照 ``_hook_sessions`` 的存活判定邏輯，不經 subprocess，隔離驗證。
    """
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "live", "/work/app", "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)

    sessions = _hook_sessions(procs_by_provider={"claude-code": None, "codex": []})

    assert sessions[0].status is Status.WAITING, "provider 掃描未知（None）不等於沒有 process，不該判 ENDED"


def _fake_run_timeout(calls: dict[str, int]) -> Callable[..., Any]:
    def fake_run(*_args: object, **_kwargs: object) -> Any:
        calls["n"] += 1
        raise subprocess.TimeoutExpired(cmd="ps", timeout=3)

    return fake_run


def _fake_run_nonzero_exit(calls: dict[str, int]) -> Callable[..., Any]:
    def fake_run(*_args: object, **_kwargs: object) -> Any:
        calls["n"] += 1
        return SimpleNamespace(stdout="", returncode=1)

    return fake_run


@pytest.mark.parametrize("make_fake_run", [_fake_run_timeout, _fake_run_nonzero_exit], ids=["timeout", "nonzero-exit"])
def test_hook_sessions_survives_ps_scan_failure_without_caching_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    make_fake_run: Any,
) -> None:
    """故障注入回歸測試（對照 ring-vanishing-sessions 診斷假說 1）。

    ``ps`` 這一輪失敗（逾時或非零 exit）時：
    (a) 既有 hook session 不會被判 ENDED——掃描失敗＝未知，不是「沒有任何 process」。
    (b) 失敗不會被快取——下一輪呼叫會真的重打一次 ``ps``，不會被 1 秒 TTL 卡住而
        繼續沿用失敗結果。

    修法前：``_ps_claude_snapshot()`` 失敗回傳 ``""`` 且快取，``running_foreground_
    claude_pids()`` 等於零個活著的 claude，``_hook_sessions`` 判定該 cwd 唯一一筆
    row 離場——這個測試在修法前會紅（session 被判 ENDED），修法後綠。
    """
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "live", "/work/app", "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)
    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr(registry, "_codex_pids_cache", (-1.0, []))
    monkeypatch.setattr(registry, "_ps_codex_snapshot_cache", (-1.0, ""))

    calls = {"n": 0}
    monkeypatch.setattr("ring.registry.subprocess.run", make_fake_run(calls))

    sessions = _hook_sessions(procs_by_provider=registry.collect_provider_procs())
    by_id = {s.session_id: s.status for s in sessions}
    assert by_id["live"] is Status.WAITING, "ps 掃描失敗這輪，既有 session 狀態應維持不變，不能判 ENDED"

    calls_after_first_round = calls["n"]
    assert calls_after_first_round >= 1, "ps 應該真的被呼叫過，不是提早短路沒測到"

    # 下一輪：不 reset 快取（模擬同一支長跑程式的下一輪刷新），若失敗被誤快取，
    # 這裡 subprocess.run 呼叫次數就不會再增加。
    _hook_sessions(procs_by_provider=registry.collect_provider_procs())
    assert calls["n"] > calls_after_first_round, "失敗不該被快取；下一輪必須重新真的呼叫 ps 才能自動復原"


def test_hook_sessions_recovers_real_end_after_ps_timeout_clears(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ps 逾時的下一輪若真的掃到零個 process，仍要正確判 ENDED——證明「未知」的保守處理
    沒有把失敗狀態誤快取住、卡死成永遠不殺，掃描一旦恢復正常就能立刻反映真實存活。
    """
    registry_dir = tmp_path / "sessions"
    _write_hook_session(registry_dir, "gone", "/work/app", "/dev/ttys001")
    monkeypatch.setattr("ring.registry.RING_REGISTRY", registry_dir)
    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr(registry, "_codex_pids_cache", (-1.0, []))
    monkeypatch.setattr(registry, "_ps_codex_snapshot_cache", (-1.0, ""))
    monkeypatch.setattr(
        "ring.registry.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired(cmd="ps", timeout=3)),
    )

    first = _hook_sessions(procs_by_provider=registry.collect_provider_procs())
    assert {s.session_id: s.status for s in first}["gone"] is Status.WAITING

    _reset_claude_ps_caches(monkeypatch)
    monkeypatch.setattr(registry, "_codex_pids_cache", (-1.0, []))
    monkeypatch.setattr(registry, "_ps_codex_snapshot_cache", (-1.0, ""))
    monkeypatch.setattr(
        "ring.registry.subprocess.run",
        lambda *a, **k: SimpleNamespace(stdout="  PID COMM ARGS\n", returncode=0),
    )

    second = _hook_sessions(procs_by_provider=registry.collect_provider_procs())
    assert {s.session_id: s.status for s in second}["gone"] is Status.ENDED, (
        "ps 恢復正常且真的沒有任何 process 時，應正確判 ENDED，不能被前一輪的失敗卡住"
    )


# ---------------------------------------------------------------------------
# Codex source helpers
# ---------------------------------------------------------------------------


def _write_codex_state(db: Path, rows: list[dict[str, object]]) -> None:
    con = sqlite3.connect(db)
    try:
        con.execute(
            """
            create table threads (
                id text primary key,
                cwd text not null,
                title text not null,
                rollout_path text not null,
                preview text not null default '',
                updated_at integer not null,
                updated_at_ms integer not null,
                archived integer not null default 0
            )
            """
        )
        con.executemany(
            """
            insert into threads
                (id, cwd, title, rollout_path, preview, updated_at, updated_at_ms, archived)
            values
                (:id, :cwd, :title, :rollout_path, :preview, :updated_at, :updated_at_ms, :archived)
            """,
            rows,
        )
        con.commit()
    finally:
        con.close()


def _write_rollout(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_codex_tail_kind_detects_waiting_after_task_complete() -> None:
    records = [
        {"type": "event_msg", "payload": {"type": "user_message", "message": "do it"}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
    ]

    assert _codex_tail_kind(records) == "waiting"


def test_codex_tail_kind_detects_working_during_tool_call() -> None:
    records = [
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        {"type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}},
    ]

    assert _codex_tail_kind(records) == "working"


def test_codex_latest_action_prefers_function_call() -> None:
    records = [{"type": "response_item", "payload": {"type": "function_call", "name": "exec_command"}}]

    assert _codex_latest_action(records, "fallback") == "→ exec_command"


def test_codex_threads_reads_state_and_marks_live_idle(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout, [{"type": "event_msg", "payload": {"type": "task_complete"}}])
    db = tmp_path / "state.sqlite"
    now_ms = int(time.time() * 1000)
    _write_codex_state(
        db,
        [
            {
                "id": "codex-session",
                "cwd": "/work/app",
                "title": "Implement thing",
                "rollout_path": str(rollout),
                "preview": "Implement thing",
                "updated_at": now_ms // 1000,
                "updated_at_ms": now_ms,
                "archived": 0,
            }
        ],
    )
    monkeypatch.setattr("ring.registry.CODEX_STATE", db)

    sessions = _codex_threads([("/work/app", "/dev/ttys003")])

    assert len(sessions) == 1
    assert sessions[0].session_id == "codex:codex-session"
    assert sessions[0].source == "codex"
    assert sessions[0].status is Status.IDLE
    assert sessions[0].tty == "/dev/ttys003"


def test_codex_threads_keeps_stale_live_thread_on_symlinked_cwd(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # thread 已超過 active window，只剩 live proc 救它；proc 的 cwd 經 lsof 是 realpath，
    # sqlite 存的卻是 symlink 字面路徑。counts 以 realpath 為鍵，6h 過濾若不 realpath
    # 比對就會誤判離場、把活著的 thread 漏抓掉。
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir)

    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout, [{"type": "event_msg", "payload": {"type": "task_complete"}}])
    db = tmp_path / "state.sqlite"
    stale_ms = int((time.time() - ACTIVE_WINDOW_SECONDS - 3600) * 1000)
    _write_codex_state(
        db,
        [
            {
                "id": "stale-live",
                "cwd": str(link_dir),  # sqlite 記字面 symlink 路徑
                "title": "Long-running",
                "rollout_path": str(rollout),
                "preview": "Long-running",
                "updated_at": stale_ms // 1000,
                "updated_at_ms": stale_ms,
                "archived": 0,
            }
        ],
    )
    monkeypatch.setattr("ring.registry.CODEX_STATE", db)

    # live proc 的 cwd 是 realpath（lsof 行為）
    sessions = _codex_threads([(str(real_dir), "/dev/ttys004")])

    assert len(sessions) == 1
    assert sessions[0].session_id == "codex:stale-live"
    assert sessions[0].status is not Status.ENDED


def test_codex_threads_hides_closed_recent_thread(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    rollout = tmp_path / "rollout.jsonl"
    _write_rollout(rollout, [{"type": "event_msg", "payload": {"type": "task_complete"}}])
    db = tmp_path / "state.sqlite"
    now_ms = int(time.time() * 1000)
    _write_codex_state(
        db,
        [
            {
                "id": "closed",
                "cwd": "/work/app",
                "title": "Old but recent",
                "rollout_path": str(rollout),
                "preview": "Old but recent",
                "updated_at": now_ms // 1000,
                "updated_at_ms": now_ms,
                "archived": 0,
            }
        ],
    )
    monkeypatch.setattr("ring.registry.CODEX_STATE", db)

    sessions = _codex_threads([])

    assert len(sessions) == 1
    assert sessions[0].status is Status.ENDED
    assert sessions[0].tty is None


def test_hook_sessions_loads_waiting_detail(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """registry 檔的 waiting_detail 要進 Session（沒有時回空字串）。"""
    monkeypatch.setattr("ring.registry.RING_REGISTRY", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "s1.json").write_text(
        json.dumps(
            {
                "session_id": "s1",
                "provider": "claude-code",
                "cwd": "/work/app",
                "status": "waiting",
                "last_active": 123.0,
                "last_action": "—",
                "waiting_kind": "permission",
                "waiting_detail": "Bash: rm -rf node_modules",
            }
        )
    )
    _write_hook_session(tmp_path, "s2", "/work/app", "")

    by_id = {s.session_id: s for s in _hook_sessions([("/work/app", "")])}
    assert by_id["s1"].waiting_kind == "permission"
    assert by_id["s1"].waiting_icon == "🔐"
    assert by_id["s1"].waiting_detail == "Bash: rm -rf node_modules"
    assert by_id["s2"].waiting_kind == ""
    assert by_id["s2"].waiting_icon == ""
    assert by_id["s2"].waiting_detail == ""
