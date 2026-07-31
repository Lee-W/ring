from types import SimpleNamespace
from typing import Any

import pytest

from ring.registry import Status
from ring.sources import local_llm


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(local_llm, "_process_cache", (-1.0, []))
    monkeypatch.setattr(local_llm, "_last_scan_error", "")


def _fake_process_scan(monkeypatch: pytest.MonkeyPatch, snapshot: str, cwds: dict[int, str]) -> list[list[str]]:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **_kwargs: object) -> Any:
        calls.append(cmd)
        return SimpleNamespace(stdout=snapshot, returncode=0)

    monkeypatch.setattr("ring.sources.local_llm.subprocess.run", fake_run)
    monkeypatch.setattr("ring.registry._pids_cwd", lambda pids: {pid: cwds[pid] for pid in pids if pid in cwds})
    return calls


def test_discovers_ollama_run_and_llama_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = "\n".join(
        [
            "101 ttys001 00:05 ollama ollama run qwen3:8b",
            "202 pts/2 01:02 llama-cli /opt/bin/llama-cli -m /models/gemma.gguf -cnv",
            "303 ttys003 00:09 llama /opt/bin/llama cli -dr ai/gemma3",
        ]
    )
    calls = _fake_process_scan(monkeypatch, snapshot, {101: "/work/a", 202: "/work/b", 303: "/work/c"})

    ollama = local_llm.ollama_source.discover()
    llama = local_llm.llama_cpp_source.discover()

    assert len([call for call in calls if call[0] == "ps"]) == 1
    assert [(s.session_id, s.cwd, s.tty, s.last_action) for s in ollama] == [
        ("ollama:pid-101", "/work/a", "/dev/ttys001", "qwen3:8b")
    ]
    assert [(s.session_id, s.cwd, s.tty, s.last_action) for s in llama] == [
        ("llama.cpp:pid-202", "/work/b", "/dev/pts/2", "/models/gemma.gguf"),
        ("llama.cpp:pid-303", "/work/c", "/dev/ttys003", "ai/gemma3"),
    ]
    assert ollama[0].status is Status.IDLE
    assert llama[0].provider == "llama.cpp"


def test_excludes_servers_and_non_tty_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = "\n".join(
        [
            "101 ?? 2-00:00:00 ollama ollama serve",
            "102 ttys001 00:03 llama-server llama-server -m model.gguf",
            "103 ?? 00:04 ollama ollama run hidden:latest",
        ]
    )
    calls = _fake_process_scan(monkeypatch, snapshot, {})

    assert local_llm.ollama_source.discover() == []
    assert local_llm.llama_cpp_source.discover() == []
    assert not any(call[0] == "lsof" for call in calls)


def test_scan_failure_is_unknown_and_contributes_no_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ring.sources.local_llm.subprocess.run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout="", returncode=1),
    )

    assert local_llm.ollama_source.discover() == []
    assert local_llm.llama_cpp_source.discover() == []


@pytest.mark.parametrize(
    ("comm", "args", "expected"),
    [
        ("/opt/homebrew/bin/ollama", "/opt/homebrew/bin/ollama run qwen3:8b", ("ollama", "qwen3:8b")),
        ("ollama", "ollama run --verbose qwen3:8b", ("ollama", "qwen3:8b")),
        ("ollama", "ollama run --think high qwen3:8b", ("ollama", "qwen3:8b")),
        ("ollama", "ollama serve", None),
        ("llama-cli", "llama-cli -hf ggml-org/gemma-GGUF:Q4_K_M", ("llama.cpp", "ggml-org/gemma-GGUF:Q4_K_M")),
        ("llama-cli", 'llama-cli --model "/models/my model.gguf" -cnv', ("llama.cpp", "/models/my model.gguf")),
        ("llama-cli", "llama-cli --docker-repo=ai/gemma3", ("llama.cpp", "ai/gemma3")),
        ("llama", "llama cli -m /models/gemma.gguf", ("llama.cpp", "/models/gemma.gguf")),
        ("llama", "llama client -hf ggml-org/gemma-GGUF", ("llama.cpp", "ggml-org/gemma-GGUF")),
        ("llama", "llama server -m /models/gemma.gguf", None),
        ("llama-server", "llama-server -m model.gguf", None),
    ],
)
def test_classify_supported_commands(comm: str, args: str, expected: tuple[str, str] | None) -> None:
    assert local_llm._classify(comm, args) == expected


@pytest.mark.parametrize(
    ("value", "seconds"),
    [("00:07", 7), ("01:02:03", 3723), ("2-03:04:05", 183845), ("bad", None)],
)
def test_elapsed_seconds(value: str, seconds: int | None) -> None:
    assert local_llm._elapsed_seconds(value) == seconds


def test_diagnostic_reports_missing_lsof(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ring.sources.local_llm.shutil.which", lambda name: "/usr/bin/ps" if name == "ps" else None)

    assert local_llm.ollama_source.diagnostic_issue() == "lsof not found"
