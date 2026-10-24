"""ring.fsutil——file_lock / atomic_write_text / backup_file / quarantine_file。"""

from __future__ import annotations

import fcntl
import stat
import tempfile
import threading
from pathlib import Path

import pytest

from ring.fsutil import atomic_write_text, backup_file, file_lock, quarantine_file


def test_atomic_write_creates_and_overwrites(tmp_path: Path) -> None:
    p = tmp_path / "sub" / "a.json"
    atomic_write_text(p, "one")
    assert p.read_text(encoding="utf-8") == "one"
    atomic_write_text(p, "兩")
    assert p.read_text(encoding="utf-8") == "兩"
    assert [f.name for f in p.parent.iterdir()] == ["a.json"]  # 沒有殘留 tmp


def test_atomic_write_preserves_mode(tmp_path: Path) -> None:
    p = tmp_path / "s.json"
    p.write_text("x", encoding="utf-8")
    p.chmod(0o600)
    atomic_write_text(p, "y")
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_atomic_write_new_file_is_world_readable(tmp_path: Path) -> None:
    p = tmp_path / "n.json"
    atomic_write_text(p, "y")
    assert stat.S_IMODE(p.stat().st_mode) == 0o644


def test_atomic_write_failure_keeps_old_content_and_cleans_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    p = tmp_path / "a.json"
    p.write_text("old", encoding="utf-8")

    def boom(self: Path, target: object) -> Path:
        raise OSError("replace failed")

    monkeypatch.setattr(Path, "replace", boom)
    with pytest.raises(OSError, match="replace failed"):
        atomic_write_text(p, "new")
    monkeypatch.undo()
    assert p.read_text(encoding="utf-8") == "old"
    assert [f.name for f in tmp_path.iterdir()] == ["a.json"]


def test_atomic_write_tmp_names_are_unique(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    real = tempfile.mkstemp

    def spy(*, dir: Path, prefix: str, suffix: str) -> tuple[int, str]:
        fd, name = real(dir=dir, prefix=prefix, suffix=suffix)
        seen.append(name)
        return fd, name

    monkeypatch.setattr(tempfile, "mkstemp", spy)
    p = tmp_path / "a.json"
    atomic_write_text(p, "1")
    atomic_write_text(p, "2")
    assert len(set(seen)) == 2


def test_file_lock_is_exclusive(tmp_path: Path) -> None:
    p = tmp_path / "data.json"
    with file_lock(p):
        lock_path = tmp_path / "data.json.lock"
        assert lock_path.exists()
        with lock_path.open("a", encoding="utf-8") as other, pytest.raises(BlockingIOError):
            fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)
    # 離開後鎖已釋放
    with lock_path.open("a", encoding="utf-8") as other:
        fcntl.flock(other, fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_file_lock_serializes_threads(tmp_path: Path) -> None:
    p = tmp_path / "counter.txt"
    p.write_text("0", encoding="utf-8")

    def bump() -> None:
        for _ in range(20):
            with file_lock(p):
                n = int(p.read_text(encoding="utf-8"))
                atomic_write_text(p, str(n + 1))

    threads = [threading.Thread(target=bump) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert p.read_text(encoding="utf-8") == "120"


def test_backup_file_overwrites_previous_bak(tmp_path: Path) -> None:
    p = tmp_path / "settings.json"
    assert backup_file(p) is None
    assert not (tmp_path / "settings.json.bak").exists()
    p.write_text("v1", encoding="utf-8")
    backup_file(p)
    p.write_text("v2", encoding="utf-8")
    bak = backup_file(p)
    assert bak == tmp_path / "settings.json.bak"
    assert bak.read_text(encoding="utf-8") == "v2"


def test_quarantine_file_keeps_content(tmp_path: Path) -> None:
    p = tmp_path / "bad.json"
    p.write_text("{oops", encoding="utf-8")
    target = quarantine_file(p, stamp="T1")
    assert target == tmp_path / "bad.json.corrupt-T1"
    assert not p.exists()
    assert target.read_text(encoding="utf-8") == "{oops"
    assert quarantine_file(p, stamp="T2") is None  # 原檔已不在


def test_atomic_write_follows_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("old", encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    atomic_write_text(link, "new")
    assert link.is_symlink()
    assert real.read_text(encoding="utf-8") == "new"
    assert sorted(f.name for f in real.parent.iterdir()) == ["settings.json"]


def test_backup_file_follows_symlink(tmp_path: Path) -> None:
    real = tmp_path / "dotfiles" / "settings.json"
    real.parent.mkdir()
    real.write_text("v1", encoding="utf-8")
    link = tmp_path / "settings.json"
    link.symlink_to(real)
    bak = backup_file(link)
    assert bak == real.with_name("settings.json.bak")
    assert bak.read_text(encoding="utf-8") == "v1"
