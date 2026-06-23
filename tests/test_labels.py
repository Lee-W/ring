from pathlib import Path

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
