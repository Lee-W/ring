import contextlib
from pathlib import Path

import pytest

import ring.labels as labels_mod
from ring.labels import get_label, load_labels, set_label


def test_load_missing_file_returns_empty(tmp_path: Path) -> None:
    assert load_labels(path=tmp_path / "labels.json") == {}


def test_set_then_get(tmp_path: Path) -> None:
    p = tmp_path / "labels.json"
    set_label("s1", "重構登入", path=p)
    assert get_label("s1", path=p) == "重構登入"
    assert load_labels(path=p) == {"s1": "重構登入"}


def test_set_trims_whitespace(tmp_path: Path) -> None:
    p = tmp_path / "labels.json"
    set_label("s1", "  hello  ", path=p)
    assert get_label("s1", path=p) == "hello"


def test_empty_label_removes(tmp_path: Path) -> None:
    p = tmp_path / "labels.json"
    set_label("s1", "x", path=p)
    set_label("s1", "   ", path=p)  # 全空白 → 移除
    assert get_label("s1", path=p) == ""
    assert load_labels(path=p) == {}


def test_update_keeps_others(tmp_path: Path) -> None:
    p = tmp_path / "labels.json"
    set_label("s1", "a", path=p)
    set_label("s2", "b", path=p)
    set_label("s1", "a2", path=p)
    assert load_labels(path=p) == {"s1": "a2", "s2": "b"}


def test_get_missing_session_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "labels.json"
    set_label("s1", "a", path=p)
    assert get_label("nope", path=p) == ""


def test_bad_json_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "labels.json"
    p.write_text("not json {{", encoding="utf-8")
    assert load_labels(path=p) == {}


def test_non_dict_json_returns_empty(tmp_path: Path) -> None:
    p = tmp_path / "labels.json"
    p.write_text('["a", "b"]', encoding="utf-8")
    assert load_labels(path=p) == {}


def test_concurrent_set_label_no_lost_update(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """確定性版本：讓所有 writer 在「讀完、寫前」會合，都讀到同一份舊狀態才寫。

    無鎖時所有人會合後各寫各的，只剩最後一筆（必紅）；有鎖時同一時間只有一個
    writer 在臨界區內，會合等不到人，逾時後各自照序寫入（必綠）。
    """
    import threading

    p = tmp_path / "labels.json"
    ids = [f"s{i}" for i in range(6)]
    barrier = threading.Barrier(len(ids))
    real_load = labels_mod.load_labels

    def load_then_rendezvous(*, path: Path | None = None) -> dict[str, str]:
        data = real_load(path=path)
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=0.2)
        return data

    monkeypatch.setattr(labels_mod, "load_labels", load_then_rendezvous)

    threads = [threading.Thread(target=set_label, args=(sid, f"label-{sid}"), kwargs={"path": p}) for sid in ids]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    monkeypatch.undo()
    assert load_labels(path=p) == {sid: f"label-{sid}" for sid in ids}
