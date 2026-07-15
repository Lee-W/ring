"""Ollama 與 llama.cpp 互動式 CLI 的 zero-config 行程來源。

兩者都沒有可供 RiNG 讀取的 session transcript；這個來源因此只承諾行程層級的
存活資訊。只收有控制終端的 ``ollama run`` / ``llama-cli``，刻意排除長駐 API
server，避免把基礎設施誤當成「需要使用者回去處理」的 session。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass

import ring.registry as registry
from ring.registry import Session, Status

_CACHE_TTL = 1.0


@dataclass(frozen=True)
class LocalLLMProcess:
    pid: int
    provider: str
    cwd: str
    tty: str
    started_at: float
    model: str


_process_cache: tuple[float, list[LocalLLMProcess] | None] = (-1.0, [])


def _elapsed_seconds(value: str) -> int | None:
    """解析 ps ``etime``（``[[dd-]hh:]mm:ss``）。"""
    try:
        day_part, clock = value.split("-", 1) if "-" in value else ("0", value)
        fields = [int(part) for part in clock.split(":")]
        if len(fields) == 2:
            hours, minutes, seconds = 0, fields[0], fields[1]
        elif len(fields) == 3:
            hours, minutes, seconds = fields
        else:
            return None
        return int(day_part) * 86400 + hours * 3600 + minutes * 60 + seconds
    except ValueError:
        return None


def _command_tokens(comm: str, args: str) -> tuple[str, list[str]]:
    """找出實際 executable 與其後參數；ps args 不保證保留 shell quoting。"""
    tokens = args.split()
    executable = os.path.basename(comm.strip())
    for index, token in enumerate(tokens):
        if os.path.basename(token) == executable:
            return executable, tokens[index + 1 :]
    return executable, tokens[1:] if tokens else []


def _classify(comm: str, args: str) -> tuple[str, str] | None:
    executable, argv = _command_tokens(comm, args)
    if executable == "ollama":
        if len(argv) < 2 or argv[0] != "run":
            return None
        return "ollama", argv[1]
    if executable == "llama-cli":
        model_flags = {"-m", "--model", "-hf", "-hfr", "--hf-repo", "-mu", "--model-url"}
        for index, token in enumerate(argv[:-1]):
            if token in model_flags:
                return "llama.cpp", argv[index + 1]
        return "llama.cpp", "llama-cli"
    return None


def _scan_processes() -> list[LocalLLMProcess] | None:
    """一次 ps + 一次批次 lsof 找出兩種本機互動 CLI；失敗以 ``None`` 表示未知。"""
    global _process_cache
    monotonic_now = time.monotonic()
    if 0 <= monotonic_now - _process_cache[0] <= _CACHE_TTL:
        return _process_cache[1]

    try:
        result = subprocess.run(
            ["ps", "-Ao", "pid=,tty=,etime=,comm=,args="],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None

    now = time.time()
    candidates: list[tuple[int, str, float, str, str]] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) < 4:
            continue
        tty = registry._normalize_tty(parts[1])
        if not tty:
            continue
        classified = _classify(parts[3], parts[4] if len(parts) == 5 else "")
        elapsed = _elapsed_seconds(parts[2])
        if classified is None or elapsed is None:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        provider, model = classified
        candidates.append((pid, tty, now - elapsed, provider, model))

    cwd_by_pid = registry._pids_cwd([pid for pid, *_rest in candidates])
    if cwd_by_pid is None:
        return None
    processes = [
        LocalLLMProcess(pid, provider, cwd, tty, started_at, model)
        for pid, tty, started_at, provider, model in candidates
        if (cwd := cwd_by_pid.get(pid, ""))
    ]
    _process_cache = (monotonic_now, processes)
    return processes


def running_pids() -> list[int]:
    """顯示用途的 live local-LLM CLI pid；掃描未知時回空清單。"""
    return [process.pid for process in (_scan_processes() or [])]


class LocalLLMSource:
    def __init__(self, provider: str) -> None:
        self.name = provider

    def discover(self) -> list[Session]:
        processes = _scan_processes()
        if processes is None:
            return []
        return [
            Session(
                session_id=f"{self.name}:pid-{process.pid}",
                cwd=process.cwd,
                status=Status.IDLE,
                last_active=process.started_at,
                last_action=process.model,
                source=self.name,
                tty=process.tty,
                provider=self.name,
                origin_cwd=process.cwd,
            )
            for process in processes
            if process.provider == self.name
        ]


ollama_source = LocalLLMSource("ollama")
llama_cpp_source = LocalLLMSource("llama.cpp")
