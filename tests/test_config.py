from pathlib import Path

from ring.config import Config, load


def test_missing_file_returns_defaults(tmp_path: Path) -> None:
    assert load(tmp_path / "nope.toml") == Config()


def test_parses_values(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text(
        'lang = "en"\n'
        "interval = 1.5\n"
        "show_all = true\n"
        "working_threshold_seconds = 30\n"
        'focusers = ["Terminal", "iTerm2"]\n'
    )
    cfg = load(p)
    assert cfg.lang == "en"
    assert cfg.interval == 1.5
    assert cfg.show_all is True
    assert cfg.working_threshold_seconds == 30
    assert cfg.focusers == ("Terminal", "iTerm2")


def test_bad_types_fall_back_to_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('interval = "fast"\nfocusers = "nope"\n')
    cfg = load(p)
    assert cfg.interval == Config().interval
    assert cfg.focusers == ()


def test_invalid_toml_returns_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text("[unclosed")
    assert load(p) == Config()


def test_colors_merge_over_defaults(tmp_path: Path) -> None:
    p = tmp_path / "config.toml"
    p.write_text('[colors]\nwaiting = "magenta"\n')
    cfg = load(p)
    assert cfg.colors["waiting"] == "magenta"  # 覆寫
    assert cfg.colors["working"] == "green"  # 未覆寫 → 預設


def test_colors_default_when_absent(tmp_path: Path) -> None:
    assert load(tmp_path / "none.toml").colors["waiting"] == "bold red"
